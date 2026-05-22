"""
Auto-mode active-window scheduler for the bluetooth manager.

Coordinates two distinct kinds of active scanning windows on AUTO-mode
scanners:

* Per-callback windows. Bleak callbacks registered with
  ``scan_interval``/``scan_duration`` cause a short active window on the
  scanner that currently sees each matched device, fired once per
  ``scan_interval`` seconds. Multiple matching callbacks for the same
  address on the same scanner coalesce into one window whose duration is
  the max of the coalesced durations.

* Global rediscovery sweeps. Every ``AUTO_REDISCOVERY_INTERVAL`` seconds
  each AUTO-mode scanner gets a ``AUTO_REDISCOVERY_SWEEP_DURATION``
  active window. Sweeps are staggered across scanners so that at most
  one scanner is mid-sweep at a time — the radio coverage gap stays
  bounded.

The scheduler is a single per-manager instance driven by one
``loop.call_at`` handle. ``on_advertisement`` is on the manager's hot
path; it must return cheaply when there are no per-device callbacks
registered.
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
    from .manager import BleakCallback, BluetoothManager
    from .models import BluetoothServiceInfoBleak


_LOGGER = logging.getLogger(__name__)


def _matches(callback: BleakCallback, service_info: BluetoothServiceInfoBleak) -> bool:
    """Return whether a service_info matches a callback's UUID filter."""
    uuids = callback.filters.get("UUIDs")
    if uuids is None:
        return True
    return bool(uuids.intersection(service_info.service_uuids))


class AutoScanScheduler:
    """Schedules on-demand active windows across AUTO-mode scanners."""

    __slots__ = (
        "_loop",
        "_manager",
        "_needs",
        "_pending_tasks",
        "_running",
        "_scanner_windows",
        "_sweep_in_flight",
        "_sweep_last_completed",
        "_tick_handle",
    )

    def __init__(self, manager: BluetoothManager) -> None:
        """Initialize the scheduler bound to a manager."""
        self._manager = manager
        # address -> {callback: next_due_loop_time}
        self._needs: dict[str, dict[BleakCallback, float]] = {}
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
        now = loop.time()
        for source in self._manager._sources:
            scanner = self._manager._sources[source]
            if scanner.requested_mode is BluetoothScanningMode.AUTO:
                self._sweep_last_completed.setdefault(source, now)
        self._reschedule()

    def stop(self) -> None:
        """Cancel any pending tick and refuse further work."""
        self._running = False
        if self._tick_handle is not None:
            self._tick_handle.cancel()
            self._tick_handle = None

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

    def remove_callback(self, callback: BleakCallback) -> None:
        """Drop per-(address, callback) tracking for a removed registration."""
        if callback.scan_interval is None:
            return
        empty_addresses: list[str] = []
        for address, callbacks in self._needs.items():
            if callback in callbacks:
                del callbacks[callback]
                if not callbacks:
                    empty_addresses.append(address)
        for address in empty_addresses:
            del self._needs[address]
        self._reschedule()

    def on_advertisement(self, service_info: BluetoothServiceInfoBleak) -> None:
        """Hot path. Record a tracking entry for any callback that wants one."""
        if not self._manager._bleak_callbacks or self._loop is None:
            return
        address = service_info.address
        existing = self._needs.get(address)
        for callback in self._manager._bleak_callbacks:
            if callback.scan_interval is None:
                continue
            if not _matches(callback, service_info):
                continue
            if existing is None:
                existing = self._needs[address] = {}
            if callback not in existing:
                # First time we see this address for this callback: fire one
                # window soon, then settle into the cadence.
                existing[callback] = self._loop.time() + callback.scan_interval
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
                # No recent sight; drop the tracking entries — they'll come
                # back the next time the device advertises.
                del self._needs[address]
                continue
            source = history.source
            if source in self._scanner_windows:
                # Scanner already busy; the next tick will retry.
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

    def _coalesce_duration(self, callbacks: list[BleakCallback]) -> float:
        """Pick the max requested duration, clamped to the configured range."""
        requested = max(
            (cb.scan_duration for cb in callbacks if cb.scan_duration is not None),
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
        try:
            await scanner.async_request_active_window(duration)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception(
                "%s: error running active window of %.1fs",
                scanner.name,
                duration,
            )
        finally:
            if sweep_source is not None:
                if self._loop is not None:
                    self._sweep_last_completed[sweep_source] = self._loop.time()
                if self._sweep_in_flight == sweep_source:
                    self._sweep_in_flight = None
                self._reschedule()
