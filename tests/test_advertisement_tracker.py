"""Test that advertising interval tracking is properly cleared when scanner pauses."""

import pytest

from habluetooth.advertisement_tracker import AdvertisementTracker
from habluetooth.base_scanner import BaseHaScanner
from habluetooth.central_manager import get_manager


@pytest.mark.asyncio
async def test_scanner_paused_clears_timing_data():
    """Test timing data is cleared when scanner pauses but intervals are preserved."""
    tracker = AdvertisementTracker()
    source = "test_scanner"
    address = "AA:BB:CC:DD:EE:FF"

    # Simulate collecting timing data
    tracker.sources[address] = source
    tracker._timings[address] = [1.0, 2.0, 3.0]  # Some timing data
    tracker.intervals[address] = 10.0  # Already learned interval

    # Call async_scanner_paused
    tracker.async_scanner_paused(source)

    # Check that timing data is cleared but interval is preserved
    assert address not in tracker._timings
    assert tracker.intervals[address] == 10.0  # Interval should still be there
    assert tracker.sources[address] == source  # Source mapping should still be there


@pytest.mark.asyncio
async def test_scanner_paused_only_affects_matching_source():
    """Test that pausing only affects devices from the matching source."""
    tracker = AdvertisementTracker()
    source1 = "scanner1"
    source2 = "scanner2"
    address1 = "AA:BB:CC:DD:EE:01"
    address2 = "AA:BB:CC:DD:EE:02"

    # Set up data for two sources
    tracker.sources[address1] = source1
    tracker.sources[address2] = source2
    tracker._timings[address1] = [1.0, 2.0]
    tracker._timings[address2] = [1.0, 2.0]
    tracker.intervals[address1] = 5.0
    tracker.intervals[address2] = 6.0

    # Pause only source1
    tracker.async_scanner_paused(source1)

    # Check that only source1 timing is cleared
    assert address1 not in tracker._timings
    assert address2 in tracker._timings  # source2 should still have timing data
    assert tracker.intervals[address1] == 5.0  # Intervals preserved
    assert tracker.intervals[address2] == 6.0


@pytest.mark.asyncio
async def test_connection_clears_timing_data():
    """Test that timing data is cleared when a connection is initiated."""
    # Get the manager that was set up by the fixture
    test_manager = get_manager()

    # Create actual BaseHaScanner to test the method
    real_scanner = BaseHaScanner(
        source="test_scanner", adapter="hci0", connectable=True
    )
    # BaseHaScanner gets the manager internally via get_manager()

    # Set up some timing data
    address = "AA:BB:CC:DD:EE:FF"
    test_manager._advertisement_tracker.sources[address] = real_scanner.source
    test_manager._advertisement_tracker._timings[address] = [1.0, 2.0, 3.0]
    test_manager._advertisement_tracker.intervals[address] = 10.0

    # Call _add_connecting which should clear timing data
    real_scanner._add_connecting(address)

    # Verify timing data was cleared but interval preserved
    assert address not in test_manager._advertisement_tracker._timings
    assert test_manager._advertisement_tracker.intervals.get(address) == 10.0
    assert address in real_scanner._connect_in_progress
