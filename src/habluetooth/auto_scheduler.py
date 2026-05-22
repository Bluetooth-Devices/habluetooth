"""
Auto-mode active-window scheduler.

One ``_ScannerWorker`` task per AUTO scanner sleeps on an
``asyncio.Event`` with a ``wait_for`` timeout until the next due event;
per-address registrations fire scan_interval/scan_duration windows on
the scanner currently seeing the device, and each scanner sweeps once
``AUTO_INITIAL_SWEEP_DELAY`` after joining then every
``AUTO_REDISCOVERY_INTERVAL`` thereafter, serialized across scanners.
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

# Locally aliased so the Cython .pxd can declare them as C-typed constants;
# the unaliased names stay importable from this module for Python callers.
_AUTO_INITIAL_SWEEP_DELAY = AUTO_INITIAL_SWEEP_DELAY
_AUTO_REDISCOVERY_INTERVAL = AUTO_REDISCOVERY_INTERVAL
_AUTO_REDISCOVERY_SWEEP_DURATION = AUTO_REDISCOVERY_SWEEP_DURATION
_AUTO_WINDOW_MAX_DURATION = AUTO_WINDOW_MAX_DURATION
_AUTO_WINDOW_MIN_DURATION = AUTO_WINDOW_MIN_DURATION

if TYPE_CHECKING:
    from .base_scanner import BaseHaScanner
    from .manager import BluetoothManager
    from .models import BluetoothServiceInfoBleak


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
        "_scanner",
        "_scheduler",
        "_sweep_last_completed",
        "_task",
        "_wake",
        "_window_end",
    )

    def __init__(self, scheduler: AutoScanScheduler, scanner: BaseHaScanner) -> None:
        self._scheduler = scheduler
        self._scanner = scanner
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._window_end: float = 0.0
        self._sweep_last_completed: float = 0.0

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Start the worker task; first sweep AUTO_INITIAL_SWEEP_DELAY out."""
        self._sweep_last_completed = (
            loop.time() + _AUTO_INITIAL_SWEEP_DELAY - _AUTO_REDISCOVERY_INTERVAL
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
        """Return the earliest loop-time at which this worker has work."""
        if self._window_end > now:
            return self._window_end
        next_at = self._sweep_last_completed + _AUTO_REDISCOVERY_INTERVAL
        source = self._scanner.source
        needs = self._scheduler._needs
        last_service_info = self._scheduler._manager.async_last_service_info
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

    async def _tick(self) -> None:
        """Fire due per-device windows, then the sweep."""
        loop = self._scheduler._loop
        if loop is None:
            return
        if self._window_end > loop.time():
            return
        self._window_end = 0.0
        await self._dispatch_per_device()
        await self._dispatch_sweep()

    async def _dispatch_per_device(self) -> None:
        """Fire per-(address, request) needs that target this scanner."""
        loop = self._scheduler._loop
        if loop is None:
            return
        source = self._scanner.source
        needs = self._scheduler._needs
        last_service_info = self._scheduler._manager.async_last_service_info
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
            now = loop.time()
            due = [r for r, t in entries.items() if t <= now]
            if not due:
                continue
            duration = self._scheduler._coalesce_duration(due)
            self._window_end = now + duration
            await self._run_window(duration)
            now = loop.time()
            # Re-check membership: remove_request may have dropped any of
            # the due entries while we were awaiting the window, and we
            # don't want to resurrect a cancelled registration.
            for request in due:
                if request in entries:
                    entries[request] = now + request.scan_interval
            self._window_end = 0.0

    async def _dispatch_sweep(self) -> None:
        """Fire the global rediscovery sweep if due."""
        loop = self._scheduler._loop
        if loop is None:
            return
        now = loop.time()
        if now < self._sweep_last_completed + _AUTO_REDISCOVERY_INTERVAL:
            return
        sweep_lock = self._scheduler._sweep_lock
        if sweep_lock is None:
            return
        async with sweep_lock:
            now = loop.time()
            if now < self._sweep_last_completed + _AUTO_REDISCOVERY_INTERVAL:
                return
            duration = _AUTO_REDISCOVERY_SWEEP_DURATION
            self._window_end = now + duration
            try:
                await self._run_window(duration)
            finally:
                # Advance on failure too so a stuck scanner doesn't busy-loop.
                self._sweep_last_completed = loop.time()
                self._window_end = 0.0

    async def _run_window(self, duration: float) -> bool:
        """Ask the scanner for an active window; swallow per-call exceptions."""
        try:
            return await self._scanner.async_request_active_window(duration)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception(
                "%s: error running active window of %.1fs",
                self._scanner.name,
                duration,
            )
            return False


class AutoScanScheduler:
    """Coordinates on-demand active windows across AUTO-mode scanners."""

    __slots__ = (
        "_loop",
        "_manager",
        "_needs",
        "_requests_by_address",
        "_running",
        "_sweep_lock",
        "_workers",
    )

    def __init__(self, manager: BluetoothManager) -> None:
        """Initialize the scheduler bound to a manager."""
        self._manager = manager
        self._requests_by_address: dict[str, set[ActiveScanRequest]] = {}
        self._needs: dict[str, dict[ActiveScanRequest, float]] = {}
        self._workers: dict[str, _ScannerWorker] = {}
        self._sweep_lock: asyncio.Lock | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running = False

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Bind to the event loop and spawn one worker per AUTO scanner."""
        self._loop = loop
        self._running = True
        self._sweep_lock = asyncio.Lock()
        for scanner in self._manager.async_current_scanners():
            if scanner.requested_mode is BluetoothScanningMode.AUTO:
                self._spawn_worker(scanner)

    def stop(self) -> None:
        """Cancel all worker tasks."""
        self._running = False
        for worker in self._workers.values():
            worker.stop()
        self._workers.clear()

    def add_scanner(self, scanner: BaseHaScanner) -> None:
        """Register an AUTO-mode scanner; spawn its worker if start() has run."""
        if scanner.requested_mode is not BluetoothScanningMode.AUTO:
            return
        if self._loop is None or scanner.source in self._workers:
            return
        self._spawn_worker(scanner)

    def remove_scanner(self, scanner: BaseHaScanner) -> None:
        """Stop the worker for a scanner leaving the manager."""
        worker = self._workers.pop(scanner.source, None)
        if worker is not None:
            worker.stop()

    def _spawn_worker(self, scanner: BaseHaScanner) -> None:
        assert self._loop is not None  # noqa: S101
        worker = _ScannerWorker(self, scanner)
        worker.start(self._loop)
        self._workers[scanner.source] = worker

    def add_request(self, request: ActiveScanRequest) -> None:
        """Register an active-scan request and wake the owning worker."""
        self._requests_by_address.setdefault(request.address, set()).add(request)
        history = self._manager.async_last_service_info(request.address, False)
        if history is not None:
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
        """Hot path. Track requests for the advertisement's address."""
        if not self._requests_by_address or self._loop is None:
            return
        address = service_info.address
        requests = self._requests_by_address.get(address)
        if requests is None:
            return
        existing = self._needs.get(address)
        added = False
        for request in requests:
            if existing is None:
                existing = self._needs[address] = {}
            if request not in existing:
                existing[request] = self._loop.time() + request.scan_interval
                added = True
        if added:
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
