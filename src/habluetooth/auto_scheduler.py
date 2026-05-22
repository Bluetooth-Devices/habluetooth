"""
Auto-mode active-window scheduler for the bluetooth manager.

Coordinates two distinct kinds of active scanning windows on AUTO-mode
scanners:

* Per-device windows. Callers (Home Assistant's bluetooth integration is
  the primary one) register an ``ActiveScanRequest`` for a specific
  address with a ``scan_interval`` and ``scan_duration``. When a matching
  advertisement arrives the scheduler asks the scanner currently seeing
  the device to flip active for the requested duration, repeating on the
  requested cadence. Multiple matching requests for the same address
  coalesce into one window using the max of their durations.

* Global rediscovery sweeps. Every ``AUTO_REDISCOVERY_INTERVAL`` seconds
  each AUTO-mode scanner gets a ``AUTO_REDISCOVERY_SWEEP_DURATION``
  active window. Sweeps are staggered across scanners so that at most one
  scanner is mid-sweep at a time, keeping the radio coverage gap bounded.

The scheduler is a single per-manager instance driven by one
``loop.call_at`` handle. ``on_advertisement`` is on the manager's hot
path; it must return cheaply when no active-scan request is registered.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from .const import (
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


_LOGGER = logging.getLogger(__name__)


class ActiveScanRequest:
    """
    A registered need for on-demand active scans on a specific address.

    Created by ``BluetoothManager.async_register_active_scan``. The scheduler
    indexes requests by ``address`` so the on_advertisement hot path is an
    O(1) dict lookup; nothing is iterated when the advertisement's address
    has no registered request.
    """

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


class AutoScanScheduler:
    """Schedules on-demand active windows across AUTO-mode scanners."""

    __slots__ = (
        "_loop",
        "_manager",
        "_needs",
        "_pending_tasks",
        "_requests_by_address",
        "_running",
        "_scanner_windows",
        "_sweep_in_flight",
        "_sweep_last_completed",
        "_tick_handle",
    )

    def __init__(self, manager: BluetoothManager) -> None:
        """Initialize the scheduler bound to a manager."""
        self._manager = manager
        # address -> registered requests for that address
        self._requests_by_address: dict[str, set[ActiveScanRequest]] = {}
        # address -> {request: next_due_loop_time}
        self._needs: dict[str, dict[ActiveScanRequest, float]] = {}
        # source -> loop time when the current window ends (0.0 = idle)
        self._scanner_windows: dict[str, float] = {}
        # source -> last sweep completion loop time
        self._sweep_last_completed: dict[str, float] = {}
        # source currently running a global sweep, or None
        self._sweep_in_flight: str | None = None
        self._tick_handle: asyncio.TimerHandle | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running = False
        self._pending_tasks: set[asyncio.Task[None]] = set()

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Bind the scheduler to the event loop and schedule the first tick."""
        self._loop = loop
        self._running = True
        # Initialize last-sweep so the first sweep is one interval out.
        # Overwrite any placeholder timestamps left by pre-start add_scanner
        # calls (which had no loop available); otherwise scanners registered
        # before async_setup would have last_sweep=0.0 and trigger an
        # immediate sweep on the first tick.
        now = loop.time()
        for source in self._manager._sources:
            scanner = self._manager._sources[source]
            if scanner.requested_mode is BluetoothScanningMode.AUTO:
                self._sweep_last_completed[source] = now
        self._reschedule()

    def stop(self) -> None:
        """Cancel any pending tick and pending window tasks."""
        self._running = False
        if self._tick_handle is not None:
            self._tick_handle.cancel()
            self._tick_handle = None
        # Cancel any window tasks in flight so they cannot call
        # async_request_active_window after shutdown has started.
        for task in self._pending_tasks:
            task.cancel()
        self._pending_tasks.clear()
        self._scanner_windows.clear()
        self._sweep_in_flight = None

    def add_scanner(self, scanner: BaseHaScanner) -> None:
        """Register an AUTO-mode scanner for the global rediscovery sweep."""
        if scanner.requested_mode is not BluetoothScanningMode.AUTO:
            return
        if self._loop is None:
            self._sweep_last_completed.setdefault(scanner.source, 0.0)
            return
        self._sweep_last_completed.setdefault(scanner.source, self._loop.time())
        self._reschedule()

    def remove_scanner(self, scanner: BaseHaScanner) -> None:
        """Drop scheduler state for a scanner that's leaving the manager."""
        self._sweep_last_completed.pop(scanner.source, None)
        self._scanner_windows.pop(scanner.source, None)
        if self._sweep_in_flight == scanner.source:
            self._sweep_in_flight = None
        self._reschedule()

    def add_request(self, request: ActiveScanRequest) -> None:
        """Register an active-scan request for its address."""
        self._requests_by_address.setdefault(request.address, set()).add(request)

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
        self._reschedule()

    def on_advertisement(self, service_info: BluetoothServiceInfoBleak) -> None:
        """Hot path. Track requests for the advertisement's address."""
        # Early return when nothing is registered. Common case, cheap.
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
            # Reschedule once after the whole batch instead of per entry; on
            # the hot path multiple registrations for the same address would
            # otherwise cancel and re-arm the tick timer N times.
            self._reschedule()

    def _reschedule(self) -> None:
        """Schedule the next tick based on the earliest pending due time."""
        if not self._running or self._loop is None:
            return
        next_event = self._next_event_time(self._loop.time())
        if self._tick_handle is not None:
            self._tick_handle.cancel()
            self._tick_handle = None
        if next_event is None:
            return
        # Add a small floor so we never spin.
        delay = max(0.05, next_event - self._loop.time())
        self._tick_handle = self._loop.call_later(delay, self._async_tick)

    def _next_event_time(self, now: float) -> float | None:
        """Return the earliest upcoming event loop-time, or None if idle."""
        candidates: list[float] = []
        for callbacks in self._needs.values():
            if callbacks:
                candidates.append(min(callbacks.values()))
        for source, last in self._sweep_last_completed.items():
            if self._sweep_in_flight == source:
                continue
            candidates.append(last + AUTO_REDISCOVERY_INTERVAL)
        if not candidates:
            return None
        return min(candidates)

    def _async_tick(self) -> None:
        """Process all due windows and reschedule."""
        self._tick_handle = None
        if not self._running or self._loop is None:
            return
        now = self._loop.time()
        # Drop expired window markers.
        for source in list(self._scanner_windows):
            if self._scanner_windows[source] <= now:
                del self._scanner_windows[source]
        self._dispatch_per_device(now)
        self._dispatch_global_sweep(now)
        self._reschedule()

    def _dispatch_per_device(self, now: float) -> None:
        """Fire windows for any (address, callback) whose due time has passed."""
        for address, callbacks in list(self._needs.items()):
            due_callbacks = [cb for cb, due in callbacks.items() if due <= now]
            if not due_callbacks:
                continue
            history = self._manager._all_history.get(address)
            if history is None:
                # No recent sight; drop the tracking entries, they'll come
                # back the next time the device advertises.
                del self._needs[address]
                continue
            source = history.source
            if (busy_end := self._scanner_windows.get(source)) is not None:
                # Scanner busy. Defer all due callbacks to just after the
                # window ends; without this the next event time stays in
                # the past and the tick re-fires every 50ms until the
                # window drains.
                deferred_due = busy_end + 0.05
                for cb in due_callbacks:
                    if callbacks[cb] < deferred_due:
                        callbacks[cb] = deferred_due
                continue
            scanner = self._manager._sources.get(source)
            if scanner is None or scanner.requested_mode is not (
                BluetoothScanningMode.AUTO
            ):
                # Not an AUTO scanner: it's already fixed-mode, so don't
                # bother requesting. Advance the next-due times so we
                # don't busy-loop on the same advertisement.
                for cb in due_callbacks:
                    interval = cb.scan_interval
                    if interval is not None:
                        callbacks[cb] = now + interval
                continue
            duration = self._coalesce_duration(due_callbacks)
            self._request_window(scanner, duration)
            for cb in due_callbacks:
                interval = cb.scan_interval
                if interval is not None:
                    callbacks[cb] = now + interval

    def _dispatch_global_sweep(self, now: float) -> None:
        """Run a rediscovery sweep on the next eligible scanner, if any."""
        if self._sweep_in_flight is not None:
            return
        eligible: str | None = None
        oldest: float = now
        for source, last in self._sweep_last_completed.items():
            if last + AUTO_REDISCOVERY_INTERVAL > now:
                continue
            if source in self._scanner_windows:
                continue
            scanner = self._manager._sources.get(source)
            if (
                scanner is None
                or scanner.requested_mode is not BluetoothScanningMode.AUTO
            ):
                continue
            if last <= oldest:
                oldest = last
                eligible = source
        if eligible is None:
            return
        scanner = self._manager._sources[eligible]
        self._sweep_in_flight = eligible
        self._request_window(
            scanner, AUTO_REDISCOVERY_SWEEP_DURATION, sweep_source=eligible
        )

    def _coalesce_duration(self, entries: list[ActiveScanRequest]) -> float:
        """Pick the max requested duration, clamped to the configured range."""
        requested = max(
            (e.scan_duration for e in entries if e.scan_duration is not None),
            default=AUTO_WINDOW_MIN_DURATION,
        )
        if requested < AUTO_WINDOW_MIN_DURATION:
            return AUTO_WINDOW_MIN_DURATION
        if requested > AUTO_WINDOW_MAX_DURATION:
            return AUTO_WINDOW_MAX_DURATION
        return requested

    def _request_window(
        self,
        scanner: BaseHaScanner,
        duration: float,
        sweep_source: str | None = None,
    ) -> None:
        """Mark the scanner busy and kick off the active-window request."""
        if self._loop is None:
            return
        self._scanner_windows[scanner.source] = self._loop.time() + duration
        task = self._loop.create_task(self._run_window(scanner, duration, sweep_source))
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    async def _run_window(
        self,
        scanner: BaseHaScanner,
        duration: float,
        sweep_source: str | None,
    ) -> None:
        """Await the scanner's active window and clear in-flight state."""
        ok = False
        try:
            ok = await scanner.async_request_active_window(duration)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception(
                "%s: error running active window of %.1fs",
                scanner.name,
                duration,
            )
        finally:
            # When the scanner could not honor the request (returned False
            # or raised), drop the busy marker now so other work for that
            # source isn't blocked for the full duration.
            if not ok and self._scanner_windows.get(scanner.source) is not None:
                del self._scanner_windows[scanner.source]
            if sweep_source is not None:
                # Update _sweep_last_completed even on failure so the next
                # sweep is a full interval out instead of immediately
                # re-eligible; otherwise _next_event_time would stay in
                # the past and the tick would re-fire every 50ms, hammering
                # the scanner with stop/start cycles.
                if self._loop is not None:
                    self._sweep_last_completed[sweep_source] = self._loop.time()
                if self._sweep_in_flight == sweep_source:
                    self._sweep_in_flight = None
                self._reschedule()
