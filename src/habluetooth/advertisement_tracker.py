"""The advertisement tracker."""

from __future__ import annotations

from typing import Any

from .models import BluetoothServiceInfoBleak

ADVERTISING_TIMES_NEEDED = 16
_ADVERTISING_TIMES_NEEDED = ADVERTISING_TIMES_NEEDED

# Each scanner may buffer incoming packets so
# we need to give a bit of leeway before we
# mark a device unavailable
TRACKER_BUFFERING_WOBBLE_SECONDS = 5


_str = str


class AdvertisementTracker:
    """Tracker to determine the interval that a device is advertising."""

    __slots__ = ("_timings", "fallback_intervals", "intervals", "sources")

    def __init__(self) -> None:
        """Initialize the tracker."""
        self.intervals: dict[str, float] = {}
        self.fallback_intervals: dict[str, float] = {}
        self.sources: dict[str, str] = {}
        self._timings: dict[str, list[float]] = {}

    def async_diagnostics(self) -> dict[str, dict[str, Any]]:
        """Return diagnostics."""
        return {
            "intervals": self.intervals,
            "fallback_intervals": self.fallback_intervals,
            "sources": self.sources,
            "timings": self._timings,
        }

    def async_collect(self, service_info: BluetoothServiceInfoBleak) -> None:
        """
        Collect timings for the tracker.

        For performance reasons, it is the responsibility of the
        caller to check if the device already has an interval set or
        the source has changed before calling this function.
        """
        self.sources[service_info.address] = service_info.source
        if not (timings := self._timings.get(service_info.address)):
            self._timings[service_info.address] = [service_info.time]
            return
        timings.append(service_info.time)
        if len(timings) != _ADVERTISING_TIMES_NEEDED:
            return

        max_time_between_advertisements = timings[1] - timings[0]
        for i in range(2, len(timings)):
            time_between_advertisements = timings[i] - timings[i - 1]
            if time_between_advertisements > max_time_between_advertisements:
                max_time_between_advertisements = time_between_advertisements

        # We now know the maximum time between advertisements
        self.intervals[service_info.address] = max_time_between_advertisements
        del self._timings[service_info.address]

    def async_remove_address(self, address: _str) -> None:
        """Remove the tracker."""
        self.intervals.pop(address, None)
        self.sources.pop(address, None)
        self._timings.pop(address, None)

    def async_remove_fallback_interval(self, address: str) -> None:
        """Remove fallback interval."""
        self.fallback_intervals.pop(address, None)

    def async_remove_source(self, source: str) -> None:
        """Remove the tracker."""
        for address, tracked_source in list(self.sources.items()):
            if tracked_source == source:
                self.async_remove_address(address)
