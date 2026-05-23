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
from typing import TYPE_CHECKING

from bleak_retry_connector import NO_RSSI_VALUE

from .const import (
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

# Retry delay when the owner is mid-connect: short enough that we
# retry shortly after a typical connect completes (~10s), long enough
# that we don't busy-loop while it's in flight.
_AUTO_CONNECTING_DEFER = 30.0


_LOGGER = logging.getLogger(__name__)


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

        O(M) over tracked addresses per wake. Fine at HA scale (a few
        dozen devices); replace with a per-worker invariant maintained
        at add_request/on_advertisement/_advance_due time if M grows.
        """
        if self._window_end > now:
            return self._window_end
        next_at = self._sweep_last_completed + _AUTO_REDISCOVERY_INTERVAL
        source = self._scanner.source
        needs = self._scheduler._needs
        last_service_info = self._manager.async_last_service_info
        for address, entries in needs.items():
            if not entries:
                continue
            history = last_service_info(address, False)
            if history is None or history.source != source:
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
    ]:
        """
        Return (due_buckets, all_due) for addresses this scanner owns.

        ``due_buckets`` is the (address, entries, due) triples to
        advance after the window; ``all_due`` is the flattened list
        used to coalesce the window duration. The address is carried
        in each tuple so the connecting-fallback path in ``_tick`` can
        look up an alternate scanner per address without re-walking
        ``_needs``. Prunes orphan ``_needs`` entries (history None) in
        passing.
        """
        source = self._scanner.source
        needs = self._scheduler._needs
        last_service_info = self._manager.async_last_service_info
        due_buckets: list[
            tuple[str, dict[ActiveScanRequest, float], list[ActiveScanRequest]]
        ] = []
        all_due: list[ActiveScanRequest] = []
        for address in list(needs):
            entries = needs.get(address)
            if not entries:
                continue
            history = last_service_info(address, False)
            if history is None:
                del needs[address]
                continue
            if history.source != source:
                continue
            due = [r for r, t in entries.items() if t <= now]
            if not due:
                continue
            due_buckets.append((address, entries, due))
            all_due.extend(due)
        return due_buckets, all_due

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
            due_buckets, all_due = self._collect_due_buckets(now)
            sweep_due = now >= self._sweep_last_completed + _AUTO_REDISCOVERY_INTERVAL
            if not all_due and not sweep_due:
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


class AutoScanScheduler:
    """Coordinates on-demand active windows across AUTO-mode scanners."""

    __slots__ = (
        "_loop",
        "_manager",
        "_needs",
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
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running = False

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
            if last_service_info(address, False) is None:
                continue
            self._seed_requests(address, requests, now)

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
        """
        self._running = False
        for worker in self._workers.values():
            worker.stop()
        self._workers.clear()
        self._needs.clear()
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
        worker = self._workers.pop(source, None)
        if worker is not None:
            worker.stop()
        last_service_info = self._manager.async_last_service_info
        for address in list(self._needs):
            history = last_service_info(address, False)
            if history is not None and history.source == source:
                del self._needs[address]

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
        self._workers[scanner.source] = worker

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
        requested = max(
            (e.scan_duration for e in entries),
            default=_AUTO_WINDOW_MIN_DURATION,
        )
        if requested < _AUTO_WINDOW_MIN_DURATION:
            return _AUTO_WINDOW_MIN_DURATION
        if requested > _AUTO_WINDOW_MAX_DURATION:
            return _AUTO_WINDOW_MAX_DURATION
        return requested
