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
    """A registered need for on-demand active scans on a specific address."""

    __slots__ = ("address", "scan_duration", "scan_interval")

    def __init__(
        self,
        address: str,
        scan_interval: float,
        scan_duration: float | None,
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
        Start the worker task; first sweep AUTO_INITIAL_SWEEP_DELAY out.

        ``initial_offset`` lets the caller stagger first sweeps across
        concurrently-registered scanners so they don't all flip ACTIVE in
        the same second; subsequent sweeps stay staggered because each
        worker advances its own clock from when its prior window finished.
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

        O(M) over every tracked address per wake. Acceptable at HA's
        typical scale (a few dozen registered devices per manager); if
        the API gets adopted by deployments with hundreds of registered
        devices, replace with a per-worker invariant maintained at
        add_request/on_advertisement/_advance_due time so this is O(1).
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
        Return (due_buckets, all_due) for every address this scanner owns.

        ``due_buckets`` is the list of (entries dict, due requests) pairs to
        advance after the window fires; ``all_due`` is the flattened list of
        every due request, used to coalesce the window duration.
        Addresses whose owning scanner is no longer known are pruned from
        ``_needs`` in passing.
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

        ``from_time`` is the timestamp the next-due is measured against;
        ``_tick`` passes the tick's start ``now`` so ``scan_interval``
        is the period between window starts. Called pre-await from
        ``_tick`` so the window's owner has already claimed the slot
        before any other worker can wake; no membership check is needed
        because nothing has yielded since ``_collect_due_buckets``
        populated due_buckets.
        """
        for entries, due in due_buckets:
            for request in due:
                entries[request] = from_time + request.scan_interval

    async def _tick(self) -> None:
        """
        Fire one coalesced window covering due per-device + sweep work.

        Collection is sync; only the scanner's active-window call is
        awaited. The window duration is the max of every due per-device
        duration and (if the sweep is due) the configured sweep duration
        so a single ACTIVE flip on the scanner catches every device it
        sees during the window. ``scan_interval`` is measured between
        window *starts* (not after each window ends), so the next due
        time advances from ``now`` (this tick's start) rather than from
        ``window_end``; the same applies to the sweep clock. The return
        value of ``async_request_active_window`` is intentionally
        ignored: even on failure we still advance by ``scan_interval``
        so a stuck scanner can't busy-loop the worker.
        """
        loop = self._scheduler._loop
        if loop is None:
            return
        now = loop.time()
        # Defense-in-depth: _tick is only ever invoked from _run on a
        # single per-worker task, and the finally below clears
        # _window_end after the await returns, so this re-entry guard
        # cannot trip on the current call path. Keep it cheap and
        # explicit so a future refactor that calls _tick from
        # elsewhere can't accidentally double-fire a window.
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
        # Advance per-device next-due times and the sweep clock BEFORE the
        # await so a concurrent worker that becomes the new owner of any
        # of these addresses mid-window (e.g. an RSSI flip on a fresh
        # advertisement) doesn't fire a duplicate window for the same
        # request. Advancing from ``now`` (not ``window_end``) makes
        # ``scan_interval`` a true period between window starts; the
        # alternative ("interval after window ends") would make the
        # effective cadence ``scan_interval + duration`` and drift with
        # the actual stop/start cost. Failure of the scanner call is
        # handled the same way as success: we still don't retry until
        # scan_interval out (or AUTO_REDISCOVERY_INTERVAL out for the
        # sweep), which prevents busy-looping the worker on a stuck
        # scanner.
        self._advance_due(due_buckets, now)
        if sweep_due:
            self._sweep_last_completed = now
        try:
            await self._scanner.async_request_active_window(duration)
        except Exception as ex:  # pylint: disable=broad-except
            # First failure per recovery-cycle gets a full traceback;
            # subsequent failures get a one-liner so a persistently
            # broken scanner doesn't spam scan_interval-cadenced stack
            # traces. The flag clears on the next successful call so a
            # later failure-after-recovery captures a stack again.
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

        Fully idempotent: if ``_running`` is already True (start was
        called previously without an intervening ``stop()``), this is a
        no-op so an accidental double-call can't bind a different loop
        to the same scheduler or re-run the replay. A genuine restart
        sequence is ``stop()`` (which sets ``_running = False``) and
        then ``start(new_loop)``, which works because ``stop()`` clears
        the workers dict.

        Replays any ``_requests_by_address`` registered before
        ``start()`` into ``_needs`` so the first window for those
        requests fires ``scan_interval`` after start (assuming the
        device is in history) instead of waiting for the next
        advertisement to bootstrap tracking. Same gating as
        ``add_request``: no seed when ``last_service_info`` is None;
        ``on_advertisement`` will bootstrap on first sight.
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
        # Replay pre-start() registrations: seed _needs for any
        # request whose address already has a last_service_info, so
        # the kick-start contract holds for embedders that register
        # before BluetoothManager.async_setup runs.
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

        Sync to match ``BluetoothManager.async_stop``. ``worker.stop()``
        calls ``task.cancel()`` but doesn't await: the cancellation
        propagates on the next event-loop iteration and the task is
        reaped by asyncio. If a worker is mid-``_tick`` (mid-await on
        ``scanner.async_request_active_window``) when stop runs, the
        scanner call may complete its current await before
        ``CancelledError`` is delivered; for HA shutdown that's harmless
        because the scanners themselves are being torn down. Callers
        outside teardown that need to know the workers have actually
        stopped should ensure the event loop runs at least one more
        iteration after this returns.
        """
        self._running = False
        for worker in self._workers.values():
            worker.stop()
        self._workers.clear()

    def add_scanner(self, scanner: BaseHaScanner) -> None:
        """
        Register an AUTO-mode scanner; spawn its worker if start() has run.

        Skips if the scheduler is not currently running. ``stop()``
        sets ``_running = False`` but leaves ``_loop`` set, so without
        this guard a scanner registered between stop and (a possible
        future) restart would spawn a worker that immediately exits
        on its next iteration when ``_running`` is checked in
        ``_run``.
        """
        if scanner.requested_mode is not BluetoothScanningMode.AUTO:
            return
        if self._loop is None or not self._running or scanner.source in self._workers:
            return
        self._spawn_worker(scanner)

    def remove_scanner(self, scanner: BaseHaScanner) -> None:
        """Stop the worker for a scanner leaving the manager."""
        worker = self._workers.pop(scanner.source, None)
        if worker is not None:
            worker.stop()

    def _spawn_worker(self, scanner: BaseHaScanner) -> None:
        assert self._loop is not None  # noqa: S101
        worker = _ScannerWorker(self, scanner, self._manager)
        # Stagger first sweeps so concurrently-registered scanners don't
        # all flip ACTIVE in the same second. Each new worker's first
        # sweep is one sweep duration later than the previous one's,
        # wrapped into the initial-sweep window so the Nth scanner's
        # first sweep is bounded to AUTO_INITIAL_SWEEP_DELAY + delay
        # rather than growing linearly with worker count. Past
        # AUTO_INITIAL_SWEEP_DELAY / SWEEP_DURATION scanners the offsets
        # start to repeat, which is fine: BLE radios don't interfere
        # when multiple are active so collisions are harmless and the
        # natural advertisement jitter spreads them out over time.
        offset = (
            len(self._workers) * _AUTO_REDISCOVERY_SWEEP_DURATION
        ) % _AUTO_INITIAL_SWEEP_DELAY
        worker.start(self._loop, offset)
        self._workers[scanner.source] = worker

    def add_request(self, request: ActiveScanRequest) -> None:
        """
        Register an active-scan request and start tracking immediately.

        If a previous advertisement for ``request.address`` is in
        ``_all_history`` when this runs, the first window fires
        ``scan_interval`` seconds after registration on the current
        owner. If the device hasn't been seen yet, no ``_needs`` entry
        is seeded (a speculative seed would just be pruned on the
        next tick because ``_collect_due_buckets`` drops addresses
        with no ``last_service_info``); ``on_advertisement`` creates
        the entry and wakes the owner's worker the first time the
        device is seen, so the first window fires ``scan_interval``
        after that advertisement instead.

        ``ActiveScanRequest`` is compared by identity, so each public
        call to ``BluetoothManager.async_register_active_scan`` creates
        a new request that contributes its own cadence to the same
        address (two callers asking for windows every 60s on the same
        device get two independent 60s cadences, not one). Adding the
        *same* request object twice is idempotent and no-ops the
        wake. Cancellation is per-registration — the callable returned
        from ``async_register_active_scan`` only removes that specific
        request, not other registrations against the same address.

        Pre-``start()`` registrations (no event loop yet) record the
        request only — no ``_needs`` entry is seeded, no wake fires.
        """
        self._requests_by_address.setdefault(request.address, set()).add(request)
        if self._loop is None:
            return
        history = self._manager.async_last_service_info(request.address, False)
        if history is None:
            # No history yet; seeding _needs would just be pruned on
            # the next tick because _collect_due_buckets drops
            # addresses with no last_service_info. on_advertisement
            # will create the entry the first time the device is seen
            # and wake the owner's worker.
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
        Hot path. Track requests for the advertisement's address.

        Always wakes the worker for ``service_info.source`` when the
        address has registered active-scan requests. The wake covers
        two cases: (1) bootstrap, when an entry is created in _needs
        because the previous owner was pruned; (2) ownership flip,
        when this scanner becomes the device's new owner and its
        worker needs to re-evaluate _next_event_at to include the
        (already-tracked) entry. A single wake() is one Event.set
        call; cheap enough to do per accepted advertisement on a
        tracked address.
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
        """Pick the max requested duration, clamped to the configured range."""
        requested = max(
            (e.scan_duration for e in entries if e.scan_duration is not None),
            default=_AUTO_WINDOW_MIN_DURATION,
        )
        if requested < _AUTO_WINDOW_MIN_DURATION:
            return _AUTO_WINDOW_MIN_DURATION
        if requested > _AUTO_WINDOW_MAX_DURATION:
            return _AUTO_WINDOW_MAX_DURATION
        return requested
