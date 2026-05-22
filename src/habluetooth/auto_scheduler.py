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

    def _collect_due_buckets(self, now: float) -> tuple[
        list[tuple[dict[ActiveScanRequest, float], list[ActiveScanRequest]]],
        list[ActiveScanRequest],
    ]:
        """
        Return (due_buckets, all_due) for addresses this scanner owns.

        ``due_buckets`` is the (entries, due) pairs to advance after
        the window; ``all_due`` is the flattened list used to coalesce
        the window duration. Prunes orphan ``_needs`` entries
        (history None) in passing.
        """
        source = self._scanner.source
        needs = self._scheduler._needs
        last_service_info = self._manager.async_last_service_info
        due_buckets: list[
            tuple[dict[ActiveScanRequest, float], list[ActiveScanRequest]]
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
            due_buckets.append((entries, due))
            all_due.extend(due)
        return due_buckets, all_due

    def _advance_due(
        self,
        due_buckets: list[
            tuple[dict[ActiveScanRequest, float], list[ActiveScanRequest]]
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
        for entries, due in due_buckets:
            for request in due:
                entries[request] = from_time + request.scan_interval

    async def _tick(self) -> None:
        """
        Fire one coalesced window covering due per-device + sweep work.

        Collection is sync; only the scanner call is awaited. The
        window duration is the max of every due per-device duration
        and (if sweep is due) the sweep duration. Next-due / sweep
        clock advance from ``now`` (tick start), not ``window_end``,
        so ``scan_interval`` is a true period between window starts
        rather than ``scan_interval + duration``. The scanner call's
        return value is ignored: we advance on failure too so a stuck
        scanner can't busy-loop the worker.
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
        due_buckets, all_due = self._collect_due_buckets(now)
        sweep_due = now >= self._sweep_last_completed + _AUTO_REDISCOVERY_INTERVAL
        if not all_due and not sweep_due:
            return
        duration = self._scheduler._coalesce_duration(all_due) if all_due else 0.0
        if sweep_due and duration < _AUTO_REDISCOVERY_SWEEP_DURATION:
            duration = _AUTO_REDISCOVERY_SWEEP_DURATION
        self._window_end = now + duration
        # Advance pre-await: a new owner that wakes mid-window must
        # see the entries already advanced, otherwise an RSSI flip
        # would let the new owner fire a duplicate window.
        self._advance_due(due_buckets, now)
        if sweep_due:
            self._sweep_last_completed = now
        try:
            await self._scanner.async_request_active_window(duration)
        except Exception as ex:  # pylint: disable=broad-except
            # First failure per recovery-cycle gets a traceback;
            # subsequent failures collapse to a one-liner so a
            # persistently broken scanner can't spam the log. Flag
            # clears on the next success so failure-after-recovery
            # captures a stack again.
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
        finally:
            self._window_end = 0.0


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
            existing = self._needs.setdefault(address, {})
            for request in requests:
                if request not in existing:
                    existing[request] = now + request.scan_interval

    def stop(self) -> None:
        """
        Cancel all worker tasks (fire-and-forget).

        Sync to match ``BluetoothManager.async_stop``;
        ``worker.stop()`` calls ``task.cancel()`` without awaiting.
        Cancellation lands on the next loop iteration; a mid-``_tick``
        scanner call may complete first. Harmless for HA shutdown.
        Callers doing an in-place restart (``stop()`` then ``start()``
        on the same scheduler) must ``await asyncio.sleep(0)`` between
        them so cancelled tasks finish their finally blocks before
        new workers spawn on the same sources; ``start()`` does not
        guard against this since HA's setup/teardown flow never
        does an in-place restart.
        """
        self._running = False
        for worker in self._workers.values():
            worker.stop()
        self._workers.clear()

    def add_scanner(self, scanner: BaseHaScanner) -> None:
        """
        Register an AUTO-mode scanner; spawn its worker if running.

        Skips when ``_running`` is False (``stop()`` leaves ``_loop``
        set, so without this guard a post-stop registration would
        spawn a worker that exits on its first iteration).
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

        First window fires ``scan_interval`` after registration on
        the current owner if history exists; otherwise
        ``on_advertisement`` bootstraps tracking on first sight (the
        first window then fires ``scan_interval`` after that ad).

        ``ActiveScanRequest`` compares by identity: each public
        ``async_register_active_scan`` call adds an independent
        cadence (two 60s registrations on the same address yield two
        independent 60s cadences). Re-adding the same object is a
        no-op; cancellation is per-registration.

        Pre-``start()`` calls record the request only; no seed, no
        wake (``start()`` replays them).
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
        existing = self._needs.get(address)
        for request in requests:
            if existing is None:
                existing = self._needs[address] = {}
            if request not in existing:
                existing[request] = self._loop.time() + request.scan_interval
        self._wake_worker(service_info.source)

    def _wake_worker(self, source: str) -> None:
        """Wake the worker for ``source`` if one is registered."""
        if (worker := self._workers.get(source)) is not None:
            worker.wake()

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
