"""Tests for marking passive devices unavailable when scanners expire them."""

from __future__ import annotations

import time

import pytest

from habluetooth import (
    BluetoothServiceInfoBleak,
    HaBluetoothConnector,
    get_manager,
)
from habluetooth.const import (
    CONNECTABLE_FALLBACK_MAXIMUM_STALE_ADVERTISEMENT_SECONDS,
    FALLBACK_MAXIMUM_STALE_ADVERTISEMENT_SECONDS,
)

from . import (
    InjectableRemoteScanner,
    MockBleakClient,
    generate_advertisement_data,
    generate_ble_device,
    patch_bluetooth_time,
)

ADDRESS = "44:44:33:11:23:45"
LEARNED_INTERVAL = 48.0
# Past the scanner's expiry, well short of UNAVAILABLE_TRACK_SECONDS
# after it: only the new path can mark the device unavailable here.
AFTER_EXPIRY = CONNECTABLE_FALLBACK_MAXIMUM_STALE_ADVERTISEMENT_SECONDS + 1


def _scanner(source: str) -> InjectableRemoteScanner:
    connector = HaBluetoothConnector(
        MockBleakClient, "mock_bleak_client", lambda: False
    )
    return InjectableRemoteScanner(source, source, connector, False)


def _advertise(scanner: InjectableRemoteScanner, now: float) -> None:
    device = generate_ble_device(ADDRESS, "sensor", {}, rssi=-60)
    adv = generate_advertisement_data(
        local_name="sensor",
        service_data={"0000fcd2-0000-1000-8000-00805f9b34fb": b"\x40\x00"},
        rssi=-60,
    )
    with patch_bluetooth_time(now):
        scanner.inject_advertisement(device, adv, now)


def _expire(scanner: InjectableRemoteScanner, now: float) -> None:
    with patch_bluetooth_time(now):
        scanner._async_expire_devices()


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_expiry_marks_tracked_device_unavailable_at_once() -> None:
    """The last scanner expiring a tracked device marks it unavailable."""
    manager = get_manager()
    scanner = _scanner("esp32_a")
    unsetup = scanner.async_setup()
    cancel = manager.async_register_scanner(scanner)
    unavailable: list[BluetoothServiceInfoBleak] = []
    untrack = manager.async_track_unavailable(
        unavailable.append, ADDRESS, connectable=False
    )

    start = time.monotonic()
    _advertise(scanner, start)
    manager._advertisement_tracker.intervals[ADDRESS] = LEARNED_INTERVAL

    _expire(scanner, start + AFTER_EXPIRY)

    assert len(unavailable) == 1
    assert unavailable[0].address == ADDRESS
    assert ADDRESS not in manager._all_history
    assert ADDRESS not in manager._advertisement_tracker.intervals

    untrack()
    cancel()
    unsetup()


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_expiry_waits_for_every_scanner() -> None:
    """A device another scanner still has stays available."""
    manager = get_manager()
    scanner_a = _scanner("esp32_a")
    scanner_b = _scanner("esp32_b")
    unsetups = [scanner_a.async_setup(), scanner_b.async_setup()]
    cancels = [
        manager.async_register_scanner(scanner_a),
        manager.async_register_scanner(scanner_b),
    ]
    unavailable: list[BluetoothServiceInfoBleak] = []
    untrack = manager.async_track_unavailable(
        unavailable.append, ADDRESS, connectable=False
    )

    start = time.monotonic()
    _advertise(scanner_a, start)
    _advertise(scanner_b, start + 20)
    manager._advertisement_tracker.intervals[ADDRESS] = LEARNED_INTERVAL

    # A drops it; B heard it 20 s later and still has it.
    _expire(scanner_a, start + AFTER_EXPIRY)
    assert unavailable == []
    assert ADDRESS in manager._all_history

    # B drops it too: now it is unavailable.
    _expire(scanner_b, start + 20 + AFTER_EXPIRY)
    assert len(unavailable) == 1

    untrack()
    for cancel in cancels:
        cancel()
    for unsetup in unsetups:
        unsetup()


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_expiry_leaves_untracked_device_to_the_sweep() -> None:
    """A device nothing tracks is left for the periodic check."""
    manager = get_manager()
    scanner = _scanner("esp32_a")
    unsetup = scanner.async_setup()
    cancel = manager.async_register_scanner(scanner)

    start = time.monotonic()
    _advertise(scanner, start)
    manager._advertisement_tracker.intervals[ADDRESS] = LEARNED_INTERVAL

    _expire(scanner, start + AFTER_EXPIRY)
    assert ADDRESS in manager._all_history

    with patch_bluetooth_time(start + AFTER_EXPIRY):
        manager._async_check_unavailable()
    assert ADDRESS not in manager._all_history

    cancel()
    unsetup()


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_expiry_respects_the_advertising_interval() -> None:
    """Without a learned interval the fallback still applies."""
    manager = get_manager()
    scanner = _scanner("esp32_a")
    unsetup = scanner.async_setup()
    cancel = manager.async_register_scanner(scanner)
    unavailable: list[BluetoothServiceInfoBleak] = []
    untrack = manager.async_track_unavailable(
        unavailable.append, ADDRESS, connectable=False
    )

    start = time.monotonic()
    _advertise(scanner, start)
    manager._advertisement_tracker.intervals.pop(ADDRESS, None)

    _expire(scanner, start + AFTER_EXPIRY)
    assert unavailable == []
    assert ADDRESS in manager._all_history

    # The periodic check still marks it once the fallback has passed.
    with patch_bluetooth_time(start + FALLBACK_MAXIMUM_STALE_ADVERTISEMENT_SECONDS + 1):
        manager._async_check_unavailable()
    assert len(unavailable) == 1

    untrack()
    cancel()
    unsetup()
