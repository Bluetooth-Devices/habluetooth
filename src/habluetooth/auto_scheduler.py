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
below). Sweeps are different and run on every AUTO scanner
independently, since their job is to find devices not yet in history.


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
                  |    3. duration = max(due durations,      |
                  |       SWEEP_DURATION if sweep_due)       |
                  |    4. _advance_due (pre-await) so the    |
                  |       new owner of any of these          |
                  |       addresses can't double-fire        |
                  |    5. ONE await:                         |
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
4. The pre-await ``_advance_due`` in step 4 of ``_tick`` prevents A
   from double-firing if the flip lands mid-window.


Invariants
==========

* At most one outstanding window per scanner (``_window_end`` guards
  re-entry into ``_tick``).
* Per-device windows fire only on the scanner whose ``source`` matches
  the device's most recent advertisement source; other scanners that
  see the same device skip it.
* Global rediscovery sweeps fire on every AUTO scanner at their own
  cadence (first sweep at ``AUTO_INITIAL_SWEEP_DELAY`` + a staggered
  offset assigned at registration, every
  ``AUTO_REDISCOVERY_INTERVAL`` afterwards).
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

# When the owner scanner is busy with a connect attempt and a sweep is
# due, defer the sweep by this many seconds so the worker retries soon
# (typical connect attempts complete in ~10s) without spinning the
# event loop.
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
        Record that this worker's scanner is mid-window via another worker.

        Called by ``_dispatch_to_fallback`` after delegating an active
        window of duration ``window_end - now`` to this worker's
        scanner. We bump ``_window_end`` so the worker's own
        ``_tick`` / ``_next_event_at`` short-circuit during the
        delegated window (no redundant per-device or sweep ticks),
        and we bump ``_sweep_last_completed`` to ``now`` so the
        worker doesn't immediately schedule a sweep on top of the
        one our delegation already covers. Both moves are bounded by
        ``max`` so a longer pre-existing window/sweep cadence isn't
        shortened.

        Safe to call synchronously from another worker's tick: the
        target worker can only mutate these fields from inside its
        own ``_tick``, and the single-loop scheduler guarantees we
        aren't racing it.
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
        Set every advanced request's next-due to from_time + scan_interval.

        ``_tick`` passes its start ``now`` so ``scan_interval`` is the
        period between window starts. Called pre-await so the owner
        has claimed the slot before any other worker can wake;
        nothing has yielded since ``_collect_due_buckets`` populated
        the buckets, so no membership re-check is needed.
        """
        for _address, entries, due in due_buckets:
            for request in due:
                entries[request] = from_time + request.scan_interval

    async def _tick(self) -> None:  # noqa: C901
        """
        Fire one coalesced window covering due per-device + sweep work.

        Collection is sync; only the scanner call is awaited. The
        window duration is the max of every due per-device duration
        and (if sweep is due) the sweep duration. Next-due / sweep
        clock advance from ``now`` (tick start), not ``window_end``,
        so ``scan_interval`` is a true period between window starts
        rather than ``scan_interval + duration``. The scanner call's
        return value is ignored: we advance on failure too so a stuck
        scanner can't busy-loop the worker. An outer except keeps
        the worker alive if the sync-phase (``_collect_due_buckets``,
        ``_advance_due``) raises unexpectedly.

        If the owner scanner is in a connect attempt at tick time, the
        radio can't service the active-window flip; we dispatch each
        due address to the best alternate scanner via
        ``_dispatch_to_fallback`` and defer any due sweep so the next
        tick retries after the connect completes.
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
                # Connect attempt in progress: this scanner's radio
                # can't service the flip. Advance the due entries
                # pre-dispatch (same reasoning as the normal path: a
                # mid-dispatch ownership flip must not let another
                # worker double-fire) and route per-address windows
                # to alternate scanners.
                self._advance_due(due_buckets, now)
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
            if sweep_due:
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
        Route per-address windows to alternate scanners, defer sweep.

        For each due address, classify via
        ``_resolve_fallback_for_address``:

        * If any non-connecting ACTIVE scanner sees the address, the
          device is already being scanned right now: drop the request
          silently (no warning, no fallback flip — the user's
          requested behavior of "consider the scan done").
        * Otherwise, if an AUTO fallback is available, group the due
          requests by fallback so a fallback that covers multiple
          addresses gets exactly one ``async_request_active_window``
          call per tick with the coalesced duration (this is what
          prevents same-scanner same-tick double-fire; concurrent
          calls across ticks are handled by the scanner's own
          ``_start_stop_lock`` / ``_active_window_handle`` extend
          logic).
        * Otherwise, warn (rate-limited via ``_warned_no_fallback``).

        Sweep is per-scanner; if due, advance ``_sweep_last_completed``
        by ``_AUTO_CONNECTING_DEFER`` so the next sweep retry fires
        after the typical connect-attempt completes rather than
        spinning the event loop.
        """
        fallback_groups: dict[str, tuple[BaseHaScanner, list[ActiveScanRequest]]] = {}
        no_fallback_addresses: list[str] = []
        resolve = self._scheduler._resolve_fallback_for_address
        exclude_source = self._scanner.source
        had_any_progress = False
        for address, _entries, due in due_buckets:
            covered, fallback = resolve(address, exclude_source)
            if covered:
                # An ACTIVE-mode scanner is already scanning this
                # address — consider the scan handled.
                had_any_progress = True
                continue
            if fallback is None:
                no_fallback_addresses.append(address)
                continue
            had_any_progress = True
            existing = fallback_groups.get(fallback.source)
            if existing is None:
                fallback_groups[fallback.source] = (fallback, list(due))
            else:
                existing[1].extend(due)
        if no_fallback_addresses:
            if not self._warned_no_fallback:
                self._warned_no_fallback = True
                _LOGGER.warning(
                    (
                        "%s: connect attempt in progress and no fallback "
                        "scanner is available for active-window scan of "
                        "%s; active scan deferred until connect completes"
                    ),
                    self._scanner.name,
                    ", ".join(no_fallback_addresses),
                )
        elif had_any_progress:
            self._warned_no_fallback = False
        if sweep_due:
            # Defer sweep so the next tick retries after the
            # connect completes; advancing _sweep_last_completed
            # places the next due time at ``now + _AUTO_CONNECTING_DEFER``
            # rather than ``now``, avoiding a busy retry loop.
            self._sweep_last_completed = (
                now - _AUTO_REDISCOVERY_INTERVAL + _AUTO_CONNECTING_DEFER
            )
            _LOGGER.debug(
                "%s: deferring rediscovery sweep while connect "
                "attempt is in progress; retrying in %.1fs",
                self._scanner.name,
                _AUTO_CONNECTING_DEFER,
            )
        coalesce_duration = self._scheduler._coalesce_duration
        workers = self._scheduler._workers
        fb_worker: _ScannerWorker | None
        for fb, fb_due in fallback_groups.values():
            duration = coalesce_duration(fb_due)
            # The fallback is about to actively scan for ``duration``
            # seconds, which covers the same ground as its own
            # periodic sweep and any of its own per-device dispatches
            # that would fire during this window. Suppress redundant
            # ticks on the fallback worker for the duration via
            # ``note_window_dispatched``. Safe to call sync from this
            # worker's tick.
            fb_worker = workers.get(fb.source)
            if fb_worker is not None:
                fb_worker.note_window_dispatched(now + duration, now)
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
        Decide how this address is serviced when the owner is mid-connect.

        Returns ``(covered, best_auto_fallback)``:

        * ``covered`` is True if some non-connecting scanner has
          ``requested_mode is ACTIVE``. ACTIVE scanners are always
          actively scanning, so the device is already being scanned
          right now: the caller should drop the request silently — no
          fallback flip, no warning.
        * ``best_auto_fallback`` is the AUTO scanner with the highest
          current advertisement RSSI for the address, excluding the
          owner and any scanner that is itself in a connect attempt.
          PASSIVE scanners are never valid fallbacks because
          ``async_request_active_window`` refuses to flip them.
        """
        covered = False
        best: BaseHaScanner | None = None
        best_rssi = -10_000
        for device in self._manager.async_scanner_devices_by_address(address, False):
            scanner = device.scanner
            if scanner.source == exclude_source:
                continue
            if scanner._connections_in_progress() > 0:
                continue
            mode = scanner.requested_mode
            if mode is BluetoothScanningMode.ACTIVE:
                # Already scanning actively: device is covered, no
                # flip needed. Keep iterating so the caller still
                # sees an AUTO fallback if one exists (the caller
                # short-circuits on ``covered`` first anyway).
                covered = True
                continue
            if mode is not BluetoothScanningMode.AUTO:
                continue
            rssi = device.advertisement.rssi
            if rssi > best_rssi:
                best_rssi = rssi
                best = scanner
        return covered, best

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
