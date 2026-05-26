"""
Auto-mode active-window scheduler.

Coordinates on-demand ACTIVE scans for AUTO-mode scanners. A scanner
defaults to PASSIVE; the manager flips it to ACTIVE for ``duration``
seconds on demand when an integration has asked for active scans on a
specific device address.

Per-device active windows fire on **exactly one** scanner at a time:
whichever scanner the manager currently considers the device's owner
(``manager.async_last_service_info(address).source``). If three other
AUTO scanners can also see the device, they stay PASSIVE for that
window. Ownership can flip across scanners over time as RSSI changes;
the next-due window then fires on the new owner (see "Migration"
below). The rediscovery sweep is a floor for AUTO scanners that
haven't active-scanned in ``AUTO_REDISCOVERY_INTERVAL`` (12 h); any
active window — per-device, sweep, or a window delegated to this
scanner by the connecting-fallback path — advances
``_sweep_last_completed``, so the sweep only fires on scanners that
would otherwise stay idle.


Flow
====

                     add_request(req)            on_advertisement(adv)
                        |                            |
                        | seed _needs[addr][req]     | seed if pruned;
                        | = now + scan_interval      | always wake
                        | wake address's owner       | adv.source's worker
                        v                            v
                  +------------------------------------------+
                  |  AutoScanScheduler                       |
                  |    _requests_by_address                  |
                  |       addr -> set of ActiveScanRequest   |
                  |    _needs                                |
                  |       addr -> {request: next_due_time}   |
                  |    _workers                              |
                  |       source -> _ScannerWorker           |
                  +------------------------------------------+
                                    |
                                    | one task per AUTO scanner
                                    v
                  +------------------------------------------+
                  |  _ScannerWorker._run loop                |
                  |                                          |
                  |    sleep on _wake with timeout =         |
                  |      _next_event_at(now) - now           |
                  |    await _tick()                         |
                  |                                          |
                  |  _tick (sync collect, one await):        |
                  |    1. _collect_due_buckets               |
                  |       skip addresses whose owner         |
                  |       (last_service_info.source) is      |
                  |       not this scanner                   |
                  |    2. sweep_due = sweep cadence elapsed  |
                  |    3. if owner has connect in progress:  |
                  |          dispatch per-address to a       |
                  |          fallback scanner; return        |
                  |    4. duration = max(due durations,      |
                  |       SWEEP_DURATION if sweep_due)       |
                  |    5. _advance_due (pre-await) so the    |
                  |       new owner of any of these          |
                  |       addresses can't double-fire;       |
                  |       _sweep_last_completed = now (any   |
                  |       active scan satisfies the floor)   |
                  |    6. ONE await:                         |
                  |       scanner.async_request_active_window|
                  +------------------------------------------+


Migration
=========

When a device moves from scanner A to scanner B (RSSI flip; manager
swaps ``_all_history[addr].source`` from A to B), the scheduler picks
up the new owner without any address-level rescheduling:

1. The manager's ``_scanner_adv_received`` updates ``_all_history``
   and then calls ``auto_scheduler.on_advertisement(service_info)``
   *before* the same-payload short-circuit, so the flip is visible to
   the scheduler even for static-payload beacons.
2. ``on_advertisement`` always calls ``_wake_worker(adv.source)`` when
   the address has registered requests. B's worker wakes up.
3. On B's next ``_tick``, ``_collect_due_buckets`` reads
   ``last_service_info(addr).source`` and sees B; the entry is
   collected and dispatched. A's worker on its own next tick sees
   ``last_service_info(addr).source != A`` and skips.
4. The pre-await ``_advance_due`` in step 5 of ``_tick`` prevents A
   from double-firing if the flip lands mid-window.


Connecting fallback
===================

A scanner mid-connect can't service the active-window mode flip
(the radio is busy). At tick time, if
``scanner._connections_in_progress() > 0``, the owner's worker
routes each due address via
``AutoScanScheduler._resolve_fallback_for_address``:

* A non-connecting ACTIVE scanner that sees the address is treated
  as "covered" — the device is already being actively scanned, no
  flip needed.
* Otherwise, the highest-RSSI non-connecting AUTO scanner that
  sees the address is picked as a fallback. Calls are coalesced
  per fallback so each scanner receives at most one
  ``async_request_active_window`` per tick.
* No usable fallback: warn (rate-limited), advance the address by
  ``_AUTO_CONNECTING_DEFER`` so the next tick retries soon after
  the connect typically completes.

``_ScannerWorker.note_window_dispatched`` is called on the fallback
worker before the await to mark its radio as currently active —
this advances both ``_window_end`` (suppressing the fallback's own
ticks during the delegated window) and ``_sweep_last_completed``
(any active window satisfies the sweep floor).


Invariants
==========

* At most one outstanding window per scanner (``_window_end`` guards
  re-entry into ``_tick``).
* Per-device windows fire only on the scanner whose ``source`` matches
  the device's most recent advertisement source; other scanners that
  see the same device skip it.
* Any active window (per-device, sweep, or delegated) advances the
  scanner's ``_sweep_last_completed`` to ``now``. The rediscovery
  sweep therefore fires only on AUTO scanners that haven't had
  *any* active scan in ``AUTO_REDISCOVERY_INTERVAL`` (12 h).
* A registration kick-starts tracking immediately; ``on_advertisement``
  is the fallback that re-creates the entry if the worker pruned it
  because the device's history was missing at tick time.
* Every accepted advertisement on a tracked address wakes the source's
  worker so an ownership flip on the same scanner triggers a
  re-evaluation of ``_next_event_at``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

from bleak_retry_connector import NO_RSSI_VALUE

from .const import (
    AUTO_COALESCE_LOOKAHEAD,
    AUTO_INITIAL_SWEEP_DELAY,
    AUTO_REDISCOVERY_INTERVAL,
    AUTO_REDISCOVERY_SWEEP_DURATION,
    AUTO_WINDOW_MAX_DURATION,
    AUTO_WINDOW_MIN_DURATION,
)
from .models import BluetoothScanningMode

if TYPE_CHECKING:
    from .base_scanner import BaseHaScanner
    from .manager import BluetoothManager
    from .models import BluetoothServiceInfoBleak

# Locally aliased so the Cython .pxd can declare them as C-typed constants;
# the unaliased names stay importable from this module for Python callers.
_AUTO_INITIAL_SWEEP_DELAY = AUTO_INITIAL_SWEEP_DELAY
_AUTO_REDISCOVERY_INTERVAL = AUTO_REDISCOVERY_INTERVAL
_AUTO_REDISCOVERY_SWEEP_DURATION = AUTO_REDISCOVERY_SWEEP_DURATION
_AUTO_WINDOW_MAX_DURATION = AUTO_WINDOW_MAX_DURATION
_AUTO_WINDOW_MIN_DURATION = AUTO_WINDOW_MIN_DURATION
_AUTO_COALESCE_LOOKAHEAD = AUTO_COALESCE_LOOKAHEAD

# Retry delay when the owner is mid-connect: short enough that we
# retry shortly after a typical connect completes (~10s), long enough
# that we don't busy-loop while it's in flight.
_AUTO_CONNECTING_DEFER = 30.0

# Joiner-extension threshold: a desired end-time within this margin
# of the in-flight window's end does not trigger an extension.
# Absorbs sub-second task-start jitter for same-duration callers.
_ON_DEMAND_EXTENSION_SLOP = 1.0


_LOGGER = logging.getLogger(__name__)


def _clamp_window_duration(duration: float) -> float:
    """Clamp a window duration into ``[MIN, MAX]``."""
    if duration < _AUTO_WINDOW_MIN_DURATION:
        return _AUTO_WINDOW_MIN_DURATION
    if duration > _AUTO_WINDOW_MAX_DURATION:
        return _AUTO_WINDOW_MAX_DURATION
    return duration


class ActiveScanRequest:
    """
    A registered need for on-demand active scans on one address.

    ``scan_interval`` and ``scan_duration`` must be finite positive
    floats. ``async_register_active_scan`` enforces this at the
    public boundary; direct constructors must honor the same contract.
    """

    __slots__ = ("address", "scan_duration", "scan_interval")

    def __init__(
        self,
        address: str,
        scan_interval: float,
        scan_duration: float,
    ) -> None:
        self.address = address
        self.scan_interval = scan_interval
        self.scan_duration = scan_duration


class _ScannerWorker:
    """One persistent task per AUTO scanner; sleeps until next due event."""

    __slots__ = (
        "_failed_window",
        "_manager",
        "_owned_needs",
        "_scanner",
        "_scheduler",
        "_sweep_last_completed",
        "_task",
        "_wake",
        "_warned_no_fallback",
        "_window_end",
    )

    def __init__(
        self,
        scheduler: AutoScanScheduler,
        scanner: BaseHaScanner,
        manager: BluetoothManager,
    ) -> None:
        self._scheduler = scheduler
        self._scanner = scanner
        self._manager = manager
        self._wake: asyncio.Event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._window_end: float = 0.0
        self._sweep_last_completed: float = 0.0
        self._failed_window: bool = False
        self._warned_no_fallback: bool = False
        # Owned subset of _needs; inner dicts aliased so in-place
        # advances stay visible. Maintained by _OwnershipIndex.
        self._owned_needs: dict[str, dict[ActiveScanRequest, float]] = {}

    def start(
        self, loop: asyncio.AbstractEventLoop, initial_offset: float = 0.0
    ) -> None:
        """
        Start the worker; first sweep at AUTO_INITIAL_SWEEP_DELAY + offset.

        ``initial_offset`` staggers first sweeps across concurrently-
        registered scanners so they don't all flip ACTIVE at once.
        """
        self._sweep_last_completed = (
            loop.time()
            + _AUTO_INITIAL_SWEEP_DELAY
            + initial_offset
            - _AUTO_REDISCOVERY_INTERVAL
        )
        self._task = loop.create_task(self._run())

    def stop(self) -> None:
        """Cancel the worker task."""
        if self._task is not None and not self._task.done():
            self._task.cancel()

    def wake(self) -> None:
        """Interrupt the worker's sleep so it re-evaluates pending work."""
        self._wake.set()

    def note_window_dispatched(self, window_end: float, now: float) -> None:
        """
        Record that another worker delegated an active window here.

        Bumps ``_window_end`` to suppress redundant ticks during the
        delegated window, and ``_sweep_last_completed`` so the window
        counts as this worker's sweep. Both use ``max`` to preserve a
        longer pre-existing value.

        Known best-effort caveats; revisit if profiling shows they
        matter:

        * If this worker is mid-``_tick`` when we set ``_window_end``,
          its ``finally`` resets ``_window_end`` to 0 on exit, wiping
          our bump. The optimization is then skipped: this worker
          ticks normally during the delegated window. Correctness is
          preserved (scanner-level ``_active_window_handle`` extends
          the radio window idempotently; ``_needs`` is advanced
          per-address by each worker on its own tick), only the
          intended "skip your own ticks during my window" hint is
          lost. ``_sweep_last_completed`` lives outside the
          ``finally`` and survives.
        * The rediscovery sweep only exists to give AUTO scanners
          that never see an active window a periodic active-scan
          floor. A fallback the dispatcher delegates to *is*
          actively scanning, so it doesn't need the floor —
          ``_sweep_last_completed`` is bumped to ``now`` on every
          delegation so its separately-scheduled 12 h sweep stays
          deferred while delegated windows are happening, which is
          the right answer regardless of how short the delegated
          window is.
        """
        if self._window_end < window_end:
            self._window_end = window_end
        if self._sweep_last_completed < now:
            self._sweep_last_completed = now

    def _next_event_at(self, now: float) -> float:
        """
        Return the earliest loop-time at which this worker has work.

        O(owned), no async_last_service_info call.
        """
        if self._window_end > now:
            return self._window_end
        next_at = self._sweep_last_completed + _AUTO_REDISCOVERY_INTERVAL
        for entries in self._owned_needs.values():
            if not entries:
                continue
            earliest = min(entries.values())
            if earliest < next_at:
                next_at = earliest
        return next_at

    async def _run(self) -> None:
        """Sleep until next event or wake, then process due work."""
        while True:
            loop = self._scheduler._loop
            if loop is None:
                return
            now = loop.time()
            next_at = self._next_event_at(now)
            self._wake.clear()
            delay = max(0.0, next_at - now)
            if delay > 0:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=delay)
            if not self._scheduler._running:
                return
            await self._tick()

    def _collect_due_buckets(
        self, now: float
    ) -> tuple[
        list[tuple[str, dict[ActiveScanRequest, float], list[ActiveScanRequest]]],
        list[ActiveScanRequest],
        bool,
    ]:
        """
        Return (due_buckets, all_due, any_immediate) for addresses owned.

        Iterates the owned view. Collects entries due within
        ``_AUTO_COALESCE_LOOKAHEAD`` so soon-due entries ride a window
        the caller opens; caller must gate on
        ``any_immediate or sweep_due``. Prunes orphans (history None)
        and resyncs on owner drift.

        Invariant: ``_AUTO_COALESCE_LOOKAHEAD > max window duration``.
        """
        source = self._scanner.source
        scheduler = self._scheduler
        ownership = scheduler._ownership
        needs = scheduler._needs
        owned = self._owned_needs
        last_service_info = self._manager.async_last_service_info
        threshold = now + _AUTO_COALESCE_LOOKAHEAD
        due_buckets: list[
            tuple[str, dict[ActiveScanRequest, float], list[ActiveScanRequest]]
        ] = []
        all_due: list[ActiveScanRequest] = []
        any_immediate = False
        for address in list(owned):
            entries = owned.get(address)
            if not entries:
                continue
            history = last_service_info(address, False)
            if history is None:
                # Orphan: history aged out.
                ownership.assign(address, None)
                needs.pop(address, None)
                continue
            if history.source != source:
                # Owner drifted; reassign and wake the new owner so it
                # can re-evaluate its next event time promptly.
                ownership.assign(address, history.source)
                scheduler._wake_worker(history.source)
                continue
            due: list[ActiveScanRequest] = []
            for r, t in entries.items():
                if t <= threshold:
                    due.append(r)
                    if t <= now:
                        any_immediate = True
            if not due:
                continue
            due_buckets.append((address, entries, due))
            all_due.extend(due)
        return due_buckets, all_due, any_immediate

    def _advance_due(
        self,
        due_buckets: list[
            tuple[str, dict[ActiveScanRequest, float], list[ActiveScanRequest]]
        ],
        from_time: float,
    ) -> None:
        """
        Advance all buckets by ``from_time + scan_interval`` (pre-await).

        Called before the scanner await so a mid-window ownership
        flip can't let a new owner double-fire.
        """
        for _address, entries, due in due_buckets:
            for request in due:
                entries[request] = from_time + request.scan_interval

    async def _tick(self) -> None:
        """
        Fire one coalesced window covering due per-device + sweep work.

        Collection is sync; only the scanner call is awaited. The
        window duration is the max of every due per-device duration
        and (if sweep is due) the sweep duration. ``scan_interval``
        runs from window start (now), not window end. Failure of the
        scanner call still advances ``_needs`` so a stuck scanner
        can't busy-loop the worker.

        If the owner is mid-connect at tick time, dispatch is routed
        to alternate scanners via ``_dispatch_to_fallback``.
        """
        loop = self._scheduler._loop
        if loop is None:
            return
        now = loop.time()
        # Defense-in-depth re-entry guard: unreachable on the current
        # call path (single per-worker task, finally clears
        # _window_end) but kept for future callers of _tick.
        if self._window_end > now:
            return
        self._window_end = 0.0
        try:
            due_buckets, all_due, any_immediate = self._collect_due_buckets(now)
            sweep_due = now >= self._sweep_last_completed + _AUTO_REDISCOVERY_INTERVAL
            # Gate on any_immediate (per-device hit now) or sweep_due
            # (12 h floor). Soon-due-only entries ride a window that
            # one of those triggers, but never trigger one alone.
            if not any_immediate and not sweep_due:
                return
            if self._scanner._connections_in_progress() > 0:
                # Per-address advance happens inside _dispatch_to_fallback
                # so no-fallback addresses get a short retry interval
                # rather than the full scan_interval.
                await self._dispatch_to_fallback(due_buckets, sweep_due, now)
                return
            duration = self._scheduler._coalesce_duration(all_due) if all_due else 0.0
            if sweep_due and duration < _AUTO_REDISCOVERY_SWEEP_DURATION:
                duration = _AUTO_REDISCOVERY_SWEEP_DURATION
            self._window_end = now + duration
            # Advance pre-await: a new owner that wakes mid-window
            # must see the entries already advanced, otherwise an
            # RSSI flip would let the new owner fire a duplicate
            # window.
            self._advance_due(due_buckets, now)
            # Any active window is functionally a sweep — the
            # rediscovery sweep exists only to give AUTO scanners
            # that haven't actively scanned in 12 h a floor, so
            # there's no point in scheduling a separate one when
            # the radio is about to scan anyway.
            self._sweep_last_completed = now
            try:
                await self._scanner.async_request_active_window(duration)
            except Exception as ex:  # pylint: disable=broad-except
                # First failure per recovery-cycle gets a traceback;
                # subsequent failures collapse to a one-liner so a
                # persistently broken scanner can't spam the log.
                # Flag clears on the next success so failure-after-
                # recovery captures a stack again.
                if self._failed_window:
                    _LOGGER.warning(
                        "%s: error running active window of %.1fs: %s",
                        self._scanner.name,
                        duration,
                        ex,
                    )
                else:
                    self._failed_window = True
                    _LOGGER.exception(
                        "%s: error running active window of %.1fs",
                        self._scanner.name,
                        duration,
                    )
            else:
                self._failed_window = False
        except Exception:  # pylint: disable=broad-except
            # Sync-phase failure (collect/advance/coalesce). Log so
            # the worker doesn't die silently, then continue.
            _LOGGER.exception(
                "%s: unexpected error in auto-window tick", self._scanner.name
            )
        finally:
            self._window_end = 0.0

    async def _dispatch_to_fallback(  # noqa: C901
        self,
        due_buckets: list[
            tuple[str, dict[ActiveScanRequest, float], list[ActiveScanRequest]]
        ],
        sweep_due: bool,
        now: float,
    ) -> None:
        """
        Owner is mid-connect: route per-address windows to alternates.

        Per-address outcomes (advance done in-line so a mid-dispatch
        ownership flip can't double-fire):

        * ACTIVE scanner sees it -> covered, advance by scan_interval.
        * AUTO fallback found -> dispatch, advance by scan_interval.
        * Neither -> warn (rate-limited), advance only by
          ``_AUTO_CONNECTING_DEFER`` so the next tick retries soon
          after the connect typically completes.

        Sweep is per-scanner; defer via ``_sweep_last_completed`` so
        the next tick retries without spinning the loop.
        """
        fallback_groups: dict[str, tuple[BaseHaScanner, list[ActiveScanRequest]]] = {}
        no_fallback_addresses: list[str] = []
        exclude_source = self._scanner.source
        had_any_progress = False
        retry_at = now + _AUTO_CONNECTING_DEFER
        for address, entries, due in due_buckets:
            covered, fallback = self._scheduler._resolve_fallback_for_address(
                address, exclude_source
            )
            if not covered and fallback is None:
                for request in due:
                    entries[request] = retry_at
                no_fallback_addresses.append(address)
                continue
            for request in due:
                entries[request] = now + request.scan_interval
            had_any_progress = True
            if fallback is None:
                continue
            existing = fallback_groups.get(fallback.source)
            if existing is None:
                fallback_groups[fallback.source] = (fallback, list(due))
            else:
                existing[1].extend(due)
        if no_fallback_addresses:
            if not self._warned_no_fallback:
                self._warned_no_fallback = True
                _LOGGER.warning(
                    "%s: connect in progress and no fallback scanner for %s;"
                    " retrying in %.1fs",
                    self._scanner.name,
                    ", ".join(no_fallback_addresses),
                    _AUTO_CONNECTING_DEFER,
                )
        elif had_any_progress:
            self._warned_no_fallback = False
        if sweep_due:
            self._sweep_last_completed = (
                now - _AUTO_REDISCOVERY_INTERVAL + _AUTO_CONNECTING_DEFER
            )
        # Entries were advanced by ``scan_interval`` and the fallback
        # worker was notified before this await; a failing dispatch
        # is treated like a successful one (no soon-retry) for the
        # same reason the owner path advances on failure — a stuck
        # fallback must not busy-loop the worker. The next normal
        # tick will pick the address up at its full cadence.
        # Dispatches are awaited sequentially on purpose: typical HA
        # setups have 0-2 fallbacks per tick, the BlueZ stop/start
        # path serializes at the daemon anyway, and a per-fallback
        # try/except keeps a stuck one from masking errors on the
        # others. ``asyncio.gather`` would parallelize but adds task
        # creation cost and ExceptionGroup handling for no win at
        # this scale.
        loop = self._scheduler._loop
        if TYPE_CHECKING:
            assert loop is not None
        workers = self._scheduler._workers
        fb_worker: _ScannerWorker | None
        for fb, fb_due in fallback_groups.values():
            duration = self._scheduler._coalesce_duration(fb_due)
            fb_worker = workers.get(fb.source)
            if fb_worker is not None:
                # Sample loop.time() per iteration: each prior
                # ``async_request_active_window`` await can take
                # seconds (scanner stop/restart on Linux), so the
                # owner's tick-start ``now`` is stale for later
                # fallbacks and would put ``_window_end`` in the
                # past — leaving the fallback worker's tick
                # suppression off during the delegated window.
                dispatch_now = loop.time()
                fb_worker.note_window_dispatched(dispatch_now + duration, dispatch_now)
            try:
                await fb.async_request_active_window(duration)
            except Exception:
                _LOGGER.exception(
                    "%s: error dispatching fallback active window of %.1fs to %s",
                    self._scanner.name,
                    duration,
                    fb.name,
                )


class _OwnershipIndex:
    """
    Per-address scanner ownership and per-worker owned-needs view.

    Single source of truth for which scanner owns each address and
    for the aliased subset of ``_needs`` each owner's worker sees on
    its hot path. Invariant: an address has an owner iff
    ``_needs[address]`` is non-empty.
    """

    __slots__ = ("_needs", "_owner_by_address", "_workers")

    def __init__(
        self,
        needs: dict[str, dict[ActiveScanRequest, float]],
        workers: dict[str, _ScannerWorker],
    ) -> None:
        """Bind to the scheduler's ``_needs`` and ``_workers`` dicts."""
        self._needs = needs
        self._workers = workers
        self._owner_by_address: dict[str, str] = {}

    def assign(self, address: str, new_source: str | None) -> None:
        """Move ownership of ``address`` to ``new_source`` (None clears)."""
        old_source = self._owner_by_address.get(address)
        if old_source == new_source:
            return
        if old_source is not None:
            old_worker = self._workers.get(old_source)
            if old_worker is not None:
                old_worker._owned_needs.pop(address, None)
        if new_source is None:
            self._owner_by_address.pop(address, None)
            return
        self._owner_by_address[address] = new_source
        entries = self._needs.get(address)
        if entries is None:
            return
        new_worker = self._workers.get(new_source)
        if new_worker is not None:
            new_worker._owned_needs[address] = entries

    def clear_source(self, source: str) -> None:
        """Drop owner mappings and ``_needs`` entries owned by ``source``."""
        owner_by_address = self._owner_by_address
        needs = self._needs
        for address in list(owner_by_address):
            if owner_by_address[address] == source:
                del owner_by_address[address]
                needs.pop(address, None)
        worker = self._workers.get(source)
        if worker is not None:
            worker._owned_needs.clear()

    def hook_worker(self, source: str) -> None:
        """Attach pre-assigned entries to a newly-registered worker."""
        worker = self._workers.get(source)
        if worker is None:
            return
        owned_needs = worker._owned_needs
        needs = self._needs
        for address, owner in self._owner_by_address.items():
            if owner == source:
                entries = needs.get(address)
                if entries is not None:
                    owned_needs[address] = entries

    def clear(self) -> None:
        """Reset the index and every worker's owned view."""
        for worker in self._workers.values():
            worker._owned_needs.clear()
        self._owner_by_address.clear()


class AutoScanScheduler:
    """Coordinates on-demand active windows across AUTO-mode scanners."""

    __slots__ = (
        "_loop",
        "_manager",
        "_needs",
        "_on_demand_sweep_end",
        "_on_demand_sweep_future",
        "_ownership",
        "_requests_by_address",
        "_running",
        "_workers",
    )

    def __init__(self, manager: BluetoothManager) -> None:
        """Initialize the scheduler bound to a manager."""
        self._manager = manager
        self._requests_by_address: dict[str, set[ActiveScanRequest]] = {}
        self._needs: dict[str, dict[ActiveScanRequest, float]] = {}
        self._workers: dict[str, _ScannerWorker] = {}
        self._ownership = _OwnershipIndex(self._needs, self._workers)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running = False
        self._on_demand_sweep_future: asyncio.Future[None] | None = None
        self._on_demand_sweep_end: float = 0.0

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """
        Bind to the event loop and spawn one worker per AUTO scanner.

        Idempotent: no-op if already running. A genuine restart is
        ``stop()`` (which flips ``_running`` to False) then
        ``start(new_loop)``. Also replays any pre-start
        ``_requests_by_address`` into ``_needs`` so embedders that
        register before ``async_setup`` still get the kick-start
        cadence; same history-gating as ``add_request``.
        """
        if self._running:
            return
        self._loop = loop
        self._running = True
        for scanner in self._manager.async_current_scanners():
            if (
                scanner.requested_mode is BluetoothScanningMode.AUTO
                and scanner.source not in self._workers
            ):
                self._spawn_worker(scanner)
        now = loop.time()
        last_service_info = self._manager.async_last_service_info
        for address, requests in self._requests_by_address.items():
            history = last_service_info(address, False)
            if history is None:
                continue
            self._seed_requests(address, requests, now)
            self._ownership.assign(address, history.source)

    def stop(self) -> None:
        """
        Cancel all worker tasks (fire-and-forget).

        Sync to match ``BluetoothManager.async_stop``;
        ``worker.stop()`` cancels without awaiting. Nulls ``_loop``
        too so post-stop ``add_request`` / ``on_advertisement`` fall
        back to the record-only path instead of seeding ``_needs``
        with timestamps from the cancelled loop. Clears ``_needs``
        so a later ``start(new_loop)`` re-seeds from
        ``_requests_by_address`` against the new loop's clock base;
        leaving stale due-times would let them fire instantly (or
        never) under a loop with a different ``time()`` origin.
        In-place restart (``stop()`` then ``start(new_loop)``)
        needs an ``await asyncio.sleep(0)`` between them so
        cancelled tasks finish before new workers spawn on the same
        sources; HA's flow never does this.

        Also resolves any in-flight on-demand sweep future since the
        leader is a caller task that ``worker.stop()`` cannot reach.
        """
        self._running = False
        for worker in self._workers.values():
            worker.stop()
        self._ownership.clear()
        self._workers.clear()
        self._needs.clear()
        # done() guard mirrors the leader's finally for symmetry;
        # a future left non-None after completion would otherwise
        # raise InvalidStateError here.
        future = self._on_demand_sweep_future
        if future is not None and not future.done():
            future.set_result(None)
        self._on_demand_sweep_future = None
        self._on_demand_sweep_end = 0.0
        self._loop = None

    def add_scanner(self, scanner: BaseHaScanner) -> None:
        """
        Register an AUTO-mode scanner; spawn its worker if running.

        Skips when ``_running`` or ``_loop`` are unset (both cleared
        by ``stop()``), so a post-stop registration doesn't spawn a
        worker that would have to exit on its first iteration.
        """
        if scanner.requested_mode is not BluetoothScanningMode.AUTO:
            return
        if self._loop is None or not self._running or scanner.source in self._workers:
            return
        self._spawn_worker(scanner)

    def remove_scanner(self, scanner: BaseHaScanner) -> None:
        """
        Stop the worker for a scanner leaving the manager.

        Also prunes ``_needs`` entries the scanner currently owns so
        a removed-and-not-rediscovered device doesn't keep a tracked
        entry pinned until the next history flip / age-out.
        """
        source = scanner.source
        self._ownership.clear_source(source)
        worker = self._workers.pop(source, None)
        if worker is not None:
            worker.stop()

    def _spawn_worker(self, scanner: BaseHaScanner) -> None:
        assert self._loop is not None  # noqa: S101
        worker = _ScannerWorker(self, scanner, self._manager)
        # Stagger first sweeps so concurrently-registered scanners
        # don't all flip ACTIVE at once. Modulo into the initial-sweep
        # window so the Nth offset is bounded; past
        # AUTO_INITIAL_SWEEP_DELAY/SWEEP_DURATION scanners offsets
        # repeat, harmless since BLE radios don't interfere when
        # multiple are active.
        offset = (
            len(self._workers) * _AUTO_REDISCOVERY_SWEEP_DURATION
        ) % _AUTO_INITIAL_SWEEP_DELAY
        worker.start(self._loop, offset)
        source = scanner.source
        self._workers[source] = worker
        # Attach entries pre-assigned before this scanner registered.
        self._ownership.hook_worker(source)

    def add_request(self, request: ActiveScanRequest) -> None:
        """
        Register an active-scan request and start tracking.

        First window fires ``scan_interval`` after registration if
        history exists; otherwise ``on_advertisement`` bootstraps on
        first sight. ``ActiveScanRequest`` compares by identity so
        each public ``async_register_active_scan`` call adds an
        independent cadence; cancellation is per-registration.
        Pre-``start()`` calls just record the request (``start()``
        replays them).
        """
        self._requests_by_address.setdefault(request.address, set()).add(request)
        if self._loop is None:
            return
        history = self._manager.async_last_service_info(request.address, False)
        if history is None:
            # No history: skip the seed (the next tick would prune
            # it anyway); on_advertisement will bootstrap on first
            # sight.
            return
        existing = self._needs.setdefault(request.address, {})
        if request in existing:
            return
        existing[request] = self._loop.time() + request.scan_interval
        self._ownership.assign(request.address, history.source)
        self._wake_worker(history.source)

    def remove_request(self, request: ActiveScanRequest) -> None:
        """Drop the request from the index and from any pending tracking."""
        if (bucket := self._requests_by_address.get(request.address)) is not None:
            bucket.discard(request)
            if not bucket:
                del self._requests_by_address[request.address]
        if (entries := self._needs.get(request.address)) is not None:
            entries.pop(request, None)
            if not entries:
                del self._needs[request.address]
                self._ownership.assign(request.address, None)

    def on_advertisement(self, service_info: BluetoothServiceInfoBleak) -> None:
        """
        Hot path. Track requests for the ad's address; wake the owner.

        Wake is unconditional (when the address has requests) so it
        covers both bootstrap (entry created) and ownership flip
        (existing entry, this scanner is now the owner and must
        re-evaluate ``_next_event_at``). ``Event.set`` is cheap
        enough to fire per tracked-address advertisement.
        """
        if not self._requests_by_address or self._loop is None:
            return
        address = service_info.address
        requests = self._requests_by_address.get(address)
        if requests is None:
            return
        self._seed_requests(address, requests, self._loop.time())
        self._ownership.assign(address, service_info.source)
        self._wake_worker(service_info.source)

    def _seed_requests(
        self,
        address: str,
        requests: set[ActiveScanRequest],
        now: float,
    ) -> None:
        """
        Insert any not-yet-tracked requests with next-due = now + interval.

        Shared by ``on_advertisement`` and the ``start()`` replay
        loop. Leaves existing entries' due times untouched.
        """
        existing = self._needs.setdefault(address, {})
        for request in requests:
            if request not in existing:
                existing[request] = now + request.scan_interval

    def _wake_worker(self, source: str) -> None:
        """Wake the worker for ``source`` if one is registered."""
        if (worker := self._workers.get(source)) is not None:
            worker.wake()

    def _resolve_fallback_for_address(
        self, address: str, exclude_source: str
    ) -> tuple[bool, BaseHaScanner | None]:
        """
        Return ``(covered, best_auto_fallback)`` for a due address.

        ``covered``: a non-connecting ACTIVE scanner sees the
        address (already actively scanned; caller drops silently).
        ``best_auto_fallback``: highest-RSSI non-connecting AUTO
        scanner seeing the address, excluding the owner. PASSIVE is
        never a valid fallback. Early-returns on the first ACTIVE
        coverage since the caller short-circuits on ``covered``.
        """
        best: BaseHaScanner | None = None
        best_rssi = 0
        for device in self._manager.async_scanner_devices_by_address(address, False):
            scanner = device.scanner
            if scanner.source == exclude_source:
                continue
            if scanner._connections_in_progress() > 0:
                continue
            mode = scanner.requested_mode
            if mode is BluetoothScanningMode.ACTIVE:
                return True, None
            if mode is not BluetoothScanningMode.AUTO:
                continue
            # adv_rssi is held as object so a None value doesn't
            # trip the int conversion that ``rssi=int`` in
            # @cython.locals would do on direct assignment.
            adv_rssi = device.advertisement.rssi
            rssi = NO_RSSI_VALUE if adv_rssi is None else adv_rssi
            if best is None or rssi > best_rssi:
                best_rssi = rssi
                best = scanner
        return False, best

    def _coalesce_duration(self, entries: list[ActiveScanRequest]) -> float:
        """
        Pick max requested duration, clamped to [MIN, MAX].

        Hot path; trusts ``scan_duration`` to be a finite positive
        float (``async_register_active_scan`` enforces this at the
        boundary).
        """
        return _clamp_window_duration(
            max(
                (e.scan_duration for e in entries),
                default=_AUTO_WINDOW_MIN_DURATION,
            )
        )

    async def _flip_scanners_for_sweep(self, duration: float) -> bool:  # noqa: C901
        """
        Flip every non-busy AUTO scanner into a ``duration``-second window.

        Returns ``True`` if at least one scanner actually opened a
        window (per-scanner result was ``True``), ``False`` if no
        AUTO workers are registered, every one is mid-connect, or
        every dispatched scanner declined / raised so the caller
        can short-circuit any post-flip sleep on a window that
        never opened.

        ``return_exceptions=True`` plus the per-scanner log keeps one
        stuck adapter from aborting the bus-wide sweep while still
        surfacing its failure. Mid-connect scanners are skipped —
        unlike the periodic ``_tick`` path this does not route to a
        fallback; on-demand is best-effort. Re-flipping with a
        longer duration extends the radio's open window in place
        (``BaseHaScanner.async_request_active_window`` contract),
        so the same helper serves both leader and joiner-extension.
        Caller must guard ``self._loop is not None``.

        Pre-await bumps ``_window_end`` to suppress the worker's own
        tick during the window; ``_sweep_last_completed`` is bumped
        post-await only on ``True`` so a declined/raised flip leaves
        the 12 h rediscovery floor unsatisfied for that scanner.
        Per-target ``_window_end`` is reverted to its pre-bump value
        on a non-``True`` result (when our bump still holds) so a
        declined / raised scanner does not stay locked out of its
        own ticks for the on-demand duration with no actual radio
        window open.

        Best-effort caveat (concurrent revert): when a leader's flip
        and a joiner's extension flip both visit the same worker and
        both decline, the leader's exact-equality revert guard sees
        the joiner's bump and skips, while the joiner's revert
        restores to its observed ``previous_window_end`` (the
        leader's bumped value). The worker stays bumped to the
        leader's intended end despite no radio window opening; it
        self-heals on the next tick at that end. Symmetric to the
        ``_window_end`` caveat in ``note_window_dispatched``.
        """
        if TYPE_CHECKING:
            assert self._loop is not None
        now = self._loop.time()
        window_end = now + duration
        targets: list[tuple[_ScannerWorker, BaseHaScanner, float]] = []
        for worker in self._workers.values():
            scanner = worker._scanner
            if scanner._connections_in_progress() > 0:
                continue
            previous_window_end = worker._window_end
            if previous_window_end < window_end:
                worker._window_end = window_end
            targets.append((worker, scanner, previous_window_end))
        if not targets:
            return False
        results = await asyncio.gather(
            *(
                scanner.async_request_active_window(duration)
                for _, scanner, _ in targets
            ),
            return_exceptions=True,
        )
        any_opened = False
        for (worker, scanner, previous_window_end), result in zip(
            targets, results, strict=True
        ):
            if result is True:
                any_opened = True
                if worker._sweep_last_completed < now:
                    worker._sweep_last_completed = now
                continue
            # No window opened for this scanner; if our bump still
            # holds, revert so the worker can tick normally. A
            # concurrent extension that pushed past us, or a _tick
            # finally that cleared to 0, is left alone.
            if worker._window_end == window_end:
                worker._window_end = previous_window_end
            if isinstance(result, Exception):
                _LOGGER.warning(
                    "%s: error running on-demand active window of %.1fs: %s",
                    scanner.name,
                    duration,
                    result,
                )
            elif isinstance(result, BaseException):
                # CancelledError etc. from a scanner that internally
                # cancelled; best-effort, log distinctly from a
                # genuine False-decline so logs do not mislead.
                _LOGGER.debug(
                    "%s: cancelled during on-demand active window of %.1fs",
                    scanner.name,
                    duration,
                )
            else:
                _LOGGER.debug(
                    "%s: declined on-demand active window of %.1fs",
                    scanner.name,
                    duration,
                )
        return any_opened

    async def async_request_active_scan(self, duration: float) -> None:
        """
        Flip every AUTO scanner to ACTIVE for ``duration`` seconds.

        Public entry is ``BluetoothManager.async_request_active_scan``
        (validates finite/positive); this method clamps to
        ``[MIN, MAX]``.

        Concurrent callers dedupe on ``_on_demand_sweep_future``
        (synchronous check-and-set, atomic under cooperative
        scheduling — exactly one window per bus). A joiner whose
        ``desired_end`` exceeds the current end extends the
        in-flight window: re-flip the scanners and push
        ``_on_demand_sweep_end`` out; the leader's sleep loop
        re-reads it on each wake, so an extension just makes the
        leader sleep again. ``_ON_DEMAND_EXTENSION_SLOP`` on the
        extension threshold absorbs task-start jitter so same-
        duration concurrent callers do not trigger bogus extensions.

        Cancellation: leader's cancel propagates to its caller and
        joiners wake to ``None`` (best-effort — they get whatever
        radio activity happened). Joiners ``await asyncio.shield``
        the future so a cancelled joiner cannot cancel the shared
        future and take down the siblings or the leader's
        ``set_result``.

        Fast-return: when the leader's flip neither opens a window
        itself (no AUTO workers, every one mid-connect, or every
        dispatched scanner declined / raised) nor sees a concurrent
        joiner that did (``_on_demand_sweep_end`` was not pushed
        past the leader's ``desired_end`` during the await), it
        skips the sleep loop and returns immediately rather than
        blocking the caller for a window that never opens. An
        extension whose re-flip opens nothing reverts its eager
        ``_on_demand_sweep_end`` push for the same reason.
        """
        # Capture loop locally so a concurrent stop() (which nulls
        # self._loop) during the sleep loop or the flip-await cannot
        # turn a re-read into AttributeError.
        loop = self._loop
        if loop is None:
            return
        duration = _clamp_window_duration(duration)
        now = loop.time()
        desired_end = now + duration
        in_flight = self._on_demand_sweep_future
        if in_flight is not None:
            if desired_end - self._on_demand_sweep_end > _ON_DEMAND_EXTENSION_SLOP:
                previous_end = self._on_demand_sweep_end
                self._on_demand_sweep_end = desired_end
                # asyncio.shield the extension flip so a cancelled
                # joiner does not leave the shared end pushed out
                # past a partial re-flip; either all non-busy
                # scanners receive the longer duration or none do.
                flipped = await asyncio.shield(
                    self._flip_scanners_for_sweep(desired_end - now)
                )
                # No scanner opened or extended a window for us
                # (every worker mid-connect, or every dispatched
                # scanner declined / raised); revert the eager push
                # so the leader does not sleep past the in-flight
                # radio window for nothing. Guarded so a peer joiner
                # that pushed end further during our shielded await
                # is not clobbered.
                if not flipped and self._on_demand_sweep_end == desired_end:
                    self._on_demand_sweep_end = previous_end
            await asyncio.shield(in_flight)
            return
        future = loop.create_future()
        self._on_demand_sweep_future = future
        self._on_demand_sweep_end = desired_end
        try:
            flipped = await self._flip_scanners_for_sweep(duration)
            if not flipped and self._on_demand_sweep_end <= desired_end:
                # No scanner opened a window bus-wide (no AUTO
                # workers, every one mid-connect, or every dispatched
                # scanner declined / raised) and no joiner that
                # interleaved during our await opened or extended a
                # window past our end; skip the sleep loop rather
                # than block callers for a window that never opens.
                # A joiner that succeeded would have pushed
                # `_on_demand_sweep_end` past `desired_end`; honor it
                # by falling through to the sleep loop so the leader
                # sleeps until that joiner's end (the joiner is
                # parked on the shared future and would otherwise be
                # cut short by the leader's finally).
                return
            while True:
                remaining = self._on_demand_sweep_end - loop.time()
                if remaining <= 0:
                    break
                await asyncio.sleep(remaining)
        finally:
            # Identity check so a stop+start(new_loop) cycle does
            # not let this orphan leader clobber the fresh state.
            if self._on_demand_sweep_future is future:
                self._on_demand_sweep_future = None
                self._on_demand_sweep_end = 0.0
            # stop() may have already resolved the future.
            if not future.done():
                future.set_result(None)

    def async_diagnostics(self) -> dict[str, Any]:
        """
        Return a snapshot of scheduler state for diagnostics.

        Per-worker timing fields and ``monotonic_time`` are raw
        ``loop.time()`` values so callers can compute deltas; before
        ``start()`` (or after ``stop()``) ``_loop`` is None and these
        are reported as 0.0.
        """
        loop = self._loop
        now = loop.time() if loop is not None else 0.0
        workers: dict[str, dict[str, Any]] = {}
        for source, worker in self._workers.items():
            workers[source] = {
                "name": worker._scanner.name,
                "window_end": worker._window_end,
                "sweep_last_completed": worker._sweep_last_completed,
                "next_sweep_at": (
                    worker._sweep_last_completed + _AUTO_REDISCOVERY_INTERVAL
                ),
                "next_event_at": (
                    worker._next_event_at(now) if loop is not None else 0.0
                ),
                "failed_window": worker._failed_window,
                "warned_no_fallback": worker._warned_no_fallback,
            }
        last_service_info = self._manager.async_last_service_info
        requests: dict[str, list[dict[str, Any]]] = {}
        for address, bucket in self._requests_by_address.items():
            entries = self._needs.get(address, {})
            history = last_service_info(address, False)
            owner_source = history.source if history is not None else None
            requests[address] = [
                {
                    "scan_interval": request.scan_interval,
                    "scan_duration": request.scan_duration,
                    "next_due": entries.get(request),
                    "owner_source": owner_source,
                }
                for request in bucket
            ]
        return {
            "running": self._running,
            "monotonic_time": now,
            "workers": workers,
            "requests": requests,
        }
