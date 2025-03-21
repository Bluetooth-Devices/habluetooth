"""Tests for the Bluetooth base scanner models."""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta

import pytest
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bluetooth_data_tools import monotonic_time_coarse

from habluetooth import (
    BaseHaRemoteScanner,
    BluetoothScanningMode,
    HaBluetoothConnector,
    HaScannerDetails,
    get_manager,
)
from habluetooth.const import (
    CONNECTABLE_FALLBACK_MAXIMUM_STALE_ADVERTISEMENT_SECONDS,
    FALLBACK_MAXIMUM_STALE_ADVERTISEMENT_SECONDS,
    SCANNER_WATCHDOG_INTERVAL,
    SCANNER_WATCHDOG_TIMEOUT,
)

from . import (
    MockBleakClient,
    async_fire_time_changed,
    generate_advertisement_data,
    generate_ble_device,
    patch_bluetooth_time,
    utcnow,
)


class FakeScanner(BaseHaRemoteScanner):
    """Fake scanner."""

    def inject_advertisement(
        self,
        device: BLEDevice,
        advertisement_data: AdvertisementData,
        now: float | None = None,
    ) -> None:
        """Inject an advertisement."""
        self._async_on_advertisement(
            device.address,
            advertisement_data.rssi,
            device.name,
            advertisement_data.service_uuids,
            advertisement_data.service_data,
            advertisement_data.manufacturer_data,
            advertisement_data.tx_power,
            {"scanner_specific_data": "test"},
            now or monotonic_time_coarse(),
        )


@pytest.mark.parametrize("name_2", [None, "w"])
@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_remote_scanner(name_2: str | None) -> None:
    """Test the remote scanner base class merges advertisement_data."""
    manager = get_manager()

    switchbot_device = generate_ble_device(
        "44:44:33:11:23:45",
        "wohand",
        {},
        rssi=-100,
    )
    switchbot_device_adv = generate_advertisement_data(
        local_name="wohand",
        service_uuids=["050a021a-0000-1000-8000-00805f9b34fb"],
        service_data={"050a021a-0000-1000-8000-00805f9b34fb": b"\n\xff"},
        manufacturer_data={1: b"\x01"},
        rssi=-100,
    )
    switchbot_device_2 = generate_ble_device(
        "44:44:33:11:23:45",
        name_2,
        {},
        rssi=-100,
    )
    switchbot_device_adv_2 = generate_advertisement_data(
        local_name=name_2,
        service_uuids=["00000001-0000-1000-8000-00805f9b34fb"],
        service_data={"00000001-0000-1000-8000-00805f9b34fb": b"\n\xff"},
        manufacturer_data={1: b"\x01", 2: b"\x02"},
        rssi=-100,
    )
    switchbot_device_3 = generate_ble_device(
        "44:44:33:11:23:45",
        "wohandlonger",
        {},
        rssi=-100,
    )
    switchbot_device_adv_3 = generate_advertisement_data(
        local_name="wohandlonger",
        service_uuids=["00000001-0000-1000-8000-00805f9b34fb"],
        service_data={"00000001-0000-1000-8000-00805f9b34fb": b"\n\xff"},
        manufacturer_data={1: b"\x01", 2: b"\x02"},
        rssi=-100,
    )
    switchbot_device_adv_4 = generate_advertisement_data(
        local_name="wohandlonger",
        service_uuids=["00000001-0000-1000-8000-00805f9b34fb"],
        service_data={"00000001-0000-1000-8000-00805f9b34fb": b"\n\xff"},
        manufacturer_data={1: b"\x04", 2: b"\x02", 3: b"\x03"},
        rssi=-100,
    )
    switchbot_device_adv_5 = generate_advertisement_data(
        local_name="wohandlonger",
        service_uuids=["00000001-0000-1000-8000-00805f9b34fb"],
        service_data={"00000001-0000-1000-8000-00805f9b34fb": b"\n\xff"},
        manufacturer_data={1: b"\x04", 2: b"\x01"},
        rssi=-100,
    )
    connector = HaBluetoothConnector(
        MockBleakClient, "mock_bleak_client", lambda: False
    )
    scanner = FakeScanner("esp32", "esp32", connector, True)
    details = scanner.details
    assert details == HaScannerDetails(
        source=scanner.source,
        connectable=scanner.connectable,
        name=scanner.name,
        adapter=scanner.adapter,
    )
    unsetup = scanner.async_setup()
    cancel = manager.async_register_scanner(scanner)

    scanner.inject_advertisement(switchbot_device, switchbot_device_adv)

    data = scanner.discovered_devices_and_advertisement_data
    discovered_device, discovered_adv_data = data[switchbot_device.address]
    assert discovered_device.address == switchbot_device.address
    assert discovered_device.name == switchbot_device.name
    assert (
        discovered_adv_data.manufacturer_data == switchbot_device_adv.manufacturer_data
    )
    assert discovered_adv_data.service_data == switchbot_device_adv.service_data
    assert discovered_adv_data.service_uuids == switchbot_device_adv.service_uuids
    scanner.inject_advertisement(switchbot_device_2, switchbot_device_adv_2)

    data = scanner.discovered_devices_and_advertisement_data
    discovered_device, discovered_adv_data = data[switchbot_device.address]
    assert discovered_device.address == switchbot_device.address
    assert discovered_device.name == switchbot_device.name
    assert discovered_adv_data.manufacturer_data == {1: b"\x01", 2: b"\x02"}
    assert discovered_adv_data.service_data == {
        "050a021a-0000-1000-8000-00805f9b34fb": b"\n\xff",
        "00000001-0000-1000-8000-00805f9b34fb": b"\n\xff",
    }
    assert set(discovered_adv_data.service_uuids) == {
        "050a021a-0000-1000-8000-00805f9b34fb",
        "00000001-0000-1000-8000-00805f9b34fb",
    }

    # The longer name should be used
    scanner.inject_advertisement(switchbot_device_3, switchbot_device_adv_3)
    assert discovered_device.name == switchbot_device_3.name

    # Inject the shorter name / None again to make
    # sure we always keep the longer name
    scanner.inject_advertisement(switchbot_device_2, switchbot_device_adv_2)
    assert discovered_device.name == switchbot_device_3.name

    scanner.inject_advertisement(switchbot_device_2, switchbot_device_adv_4)
    assert scanner.discovered_devices_and_advertisement_data[
        switchbot_device_2.address
    ][1].manufacturer_data == {1: b"\x04", 2: b"\x02", 3: b"\x03"}
    scanner.inject_advertisement(switchbot_device_2, switchbot_device_adv_5)
    assert scanner.discovered_devices_and_advertisement_data[
        switchbot_device_2.address
    ][1].manufacturer_data == {1: b"\x04", 2: b"\x01", 3: b"\x03"}

    cancel()
    unsetup()


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_remote_scanner_expires_connectable() -> None:
    """Test the remote scanner expires stale connectable data."""
    manager = get_manager()

    switchbot_device = generate_ble_device(
        "44:44:33:11:23:45",
        "wohand",
        {},
        rssi=-100,
    )
    switchbot_device_adv = generate_advertisement_data(
        local_name="wohand",
        service_uuids=[],
        manufacturer_data={1: b"\x01"},
        rssi=-100,
    )

    connector = HaBluetoothConnector(
        MockBleakClient, "mock_bleak_client", lambda: False
    )
    scanner = FakeScanner("esp32", "esp32", connector, True)
    unsetup = scanner.async_setup()
    cancel = manager.async_register_scanner(scanner)

    start_time_monotonic = time.monotonic()
    scanner.inject_advertisement(switchbot_device, switchbot_device_adv)

    devices = scanner.discovered_devices
    assert len(scanner.discovered_devices) == 1
    assert len(scanner.discovered_devices_and_advertisement_data) == 1
    assert devices[0].name == "wohand"

    expire_monotonic = (
        start_time_monotonic
        + CONNECTABLE_FALLBACK_MAXIMUM_STALE_ADVERTISEMENT_SECONDS
        + 1
    )
    expire_utc = utcnow() + timedelta(
        seconds=CONNECTABLE_FALLBACK_MAXIMUM_STALE_ADVERTISEMENT_SECONDS + 1
    )
    with patch_bluetooth_time(expire_monotonic):
        async_fire_time_changed(expire_utc)
        await asyncio.sleep(0)

    devices = scanner.discovered_devices
    assert len(scanner.discovered_devices) == 0
    assert len(scanner.discovered_devices_and_advertisement_data) == 0

    cancel()
    unsetup()


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_remote_scanner_expires_non_connectable() -> None:
    """Test the remote scanner expires stale non connectable data."""
    manager = get_manager()

    switchbot_device = generate_ble_device(
        "44:44:33:11:23:45",
        "wohand",
        {},
        rssi=-100,
    )
    switchbot_device_adv = generate_advertisement_data(
        local_name="wohand",
        service_uuids=[],
        manufacturer_data={1: b"\x01"},
        rssi=-100,
    )

    connector = HaBluetoothConnector(
        MockBleakClient, "mock_bleak_client", lambda: False
    )
    scanner = FakeScanner("esp32", "esp32", connector, True)
    unsetup = scanner.async_setup()
    cancel = manager.async_register_scanner(scanner)

    start_time_monotonic = time.monotonic()
    scanner.inject_advertisement(switchbot_device, switchbot_device_adv)

    devices = scanner.discovered_devices
    assert len(scanner.discovered_devices) == 1
    assert len(scanner.discovered_devices_and_advertisement_data) == 1
    assert len(scanner.discovered_device_timestamps) == 1
    assert len(scanner._discovered_device_timestamps) == 1
    dev_adv = scanner.get_discovered_device_advertisement_data(switchbot_device.address)
    assert dev_adv is not None
    dev, adv = dev_adv
    assert dev.name == "wohand"
    assert adv.local_name == "wohand"
    assert adv.manufacturer_data == switchbot_device_adv.manufacturer_data
    assert devices[0].name == "wohand"

    assert (
        FALLBACK_MAXIMUM_STALE_ADVERTISEMENT_SECONDS
        > CONNECTABLE_FALLBACK_MAXIMUM_STALE_ADVERTISEMENT_SECONDS
    )

    # The connectable timeout is used for all devices
    # as the manager takes care of availability and the scanner
    # if only concerned about making a connection
    expire_monotonic = (
        start_time_monotonic
        + CONNECTABLE_FALLBACK_MAXIMUM_STALE_ADVERTISEMENT_SECONDS
        + 1
    )
    expire_utc = utcnow() + timedelta(
        seconds=CONNECTABLE_FALLBACK_MAXIMUM_STALE_ADVERTISEMENT_SECONDS + 1
    )
    with patch_bluetooth_time(expire_monotonic):
        async_fire_time_changed(expire_utc)
        await asyncio.sleep(0)

    assert len(scanner.discovered_devices) == 0
    assert len(scanner.discovered_devices_and_advertisement_data) == 0
    assert len(scanner.discovered_device_timestamps) == 0
    assert len(scanner._discovered_device_timestamps) == 0
    assert (
        scanner.get_discovered_device_advertisement_data(switchbot_device.address)
        is None
    )

    expire_monotonic = (
        start_time_monotonic + FALLBACK_MAXIMUM_STALE_ADVERTISEMENT_SECONDS + 1
    )
    expire_utc = utcnow() + timedelta(
        seconds=FALLBACK_MAXIMUM_STALE_ADVERTISEMENT_SECONDS + 1
    )
    with patch_bluetooth_time(expire_monotonic):
        async_fire_time_changed(expire_utc)
        await asyncio.sleep(0)

    assert len(scanner.discovered_devices) == 0
    assert len(scanner.discovered_devices_and_advertisement_data) == 0
    assert len(scanner.discovered_device_timestamps) == 0
    assert len(scanner._discovered_device_timestamps) == 0
    assert (
        scanner.get_discovered_device_advertisement_data(switchbot_device.address)
        is None
    )

    cancel()
    unsetup()


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_base_scanner_connecting_behavior() -> None:
    """Test the default behavior is to mark the scanner as not scanning on connect."""
    manager = get_manager()

    switchbot_device = generate_ble_device(
        "44:44:33:11:23:45",
        "wohand",
        {},
        rssi=-100,
    )
    switchbot_device_adv = generate_advertisement_data(
        local_name="wohand",
        service_uuids=[],
        manufacturer_data={1: b"\x01"},
        rssi=-100,
    )

    connector = HaBluetoothConnector(
        MockBleakClient, "mock_bleak_client", lambda: False
    )
    scanner = FakeScanner("esp32", "esp32", connector, True)
    unsetup = scanner.async_setup()
    cancel = manager.async_register_scanner(scanner)

    with scanner.connecting():
        assert scanner.scanning is False

        # We should still accept new advertisements while connecting
        # since advertisements are delivered asynchronously and
        # we don't want to miss any even when we are willing to
        # accept advertisements from another scanner in the brief window
        # between when we start connecting and when we stop scanning
        scanner.inject_advertisement(switchbot_device, switchbot_device_adv)

    devices = scanner.discovered_devices
    assert len(scanner.discovered_devices) == 1
    assert len(scanner.discovered_devices_and_advertisement_data) == 1
    assert devices[0].name == "wohand"

    cancel()
    unsetup()


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_scanner_stops_responding() -> None:
    """Test we mark a scanner are not scanning when it stops responding."""
    manager = get_manager()

    connector = HaBluetoothConnector(
        MockBleakClient, "mock_bleak_client", lambda: False
    )
    scanner = FakeScanner(
        "esp32",
        "esp32",
        connector,
        True,
        current_mode=BluetoothScanningMode.ACTIVE,
        requested_mode=BluetoothScanningMode.ACTIVE,
    )
    unsetup = scanner.async_setup()
    cancel = manager.async_register_scanner(scanner)

    start_time_monotonic = time.monotonic()

    assert scanner.scanning is True
    failure_reached_time = (
        start_time_monotonic
        + SCANNER_WATCHDOG_TIMEOUT
        + SCANNER_WATCHDOG_INTERVAL.total_seconds()
    )
    # We hit the timer with no detections,
    # so we reset the adapter and restart the scanner
    with patch_bluetooth_time(failure_reached_time):
        async_fire_time_changed(utcnow() + SCANNER_WATCHDOG_INTERVAL)
        await asyncio.sleep(0)

    assert scanner.scanning is False

    bparasite_device = generate_ble_device(  # type: ignore[unreachable]
        "44:44:33:11:23:45",
        "bparasite",
        {},
        rssi=-100,
    )
    bparasite_device_adv = generate_advertisement_data(
        local_name="bparasite",
        service_uuids=[],
        manufacturer_data={1: b"\x01"},
        rssi=-100,
    )

    failure_reached_time += 1

    with patch_bluetooth_time(failure_reached_time):
        scanner.inject_advertisement(
            bparasite_device, bparasite_device_adv, failure_reached_time
        )

    # As soon as we get a detection, we know the scanner is working again
    assert scanner.scanning is True
    assert scanner.requested_mode == BluetoothScanningMode.ACTIVE
    assert scanner.current_mode == BluetoothScanningMode.ACTIVE

    cancel()
    unsetup()


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_merge_manufacturer_data_history_existing() -> None:
    """Test merging manufacturer data history."""
    manager = get_manager()

    sensor_push_device = generate_ble_device(
        "44:44:33:11:23:45",
        "",
        {},
        rssi=-60,
    )
    sensor_push_device_adv = generate_advertisement_data(
        local_name="",
        rssi=-60,
        manufacturer_data={
            64256: b"B\r.\xa9\xb6",
            31488: b"\x98\xfa\xb6\x91\xb6",
        },
        service_uuids=["ef090000-11d6-42ba-93b8-9dd7ec090ab0"],
        service_data={},
    )

    sensor_push_adv_2 = generate_advertisement_data(
        local_name="",
        service_uuids=["ef090000-11d6-42ba-93b8-9dd7ec090ab0"],
        service_data={},
        manufacturer_data={
            31488: b"\x98\xfa\xb6\x91\xb6",
        },
        rssi=-100,
    )

    connector = HaBluetoothConnector(
        MockBleakClient, "mock_bleak_client", lambda: False
    )
    scanner = FakeScanner("esp32", "esp32", connector, True)
    details = scanner.details
    assert details == HaScannerDetails(
        source=scanner.source,
        connectable=scanner.connectable,
        name=scanner.name,
        adapter=scanner.adapter,
    )
    unsetup = scanner.async_setup()
    cancel = manager.async_register_scanner(scanner)

    scanner.inject_advertisement(sensor_push_device, sensor_push_device_adv)

    data = scanner.discovered_devices_and_advertisement_data
    discovered_device, discovered_adv_data = data[sensor_push_device.address]
    assert discovered_device.address == sensor_push_device.address
    assert discovered_device.name == sensor_push_device.name
    assert (
        discovered_adv_data.manufacturer_data
        == sensor_push_device_adv.manufacturer_data
    )
    assert discovered_adv_data.service_data == sensor_push_device_adv.service_data
    assert discovered_adv_data.service_uuids == sensor_push_device_adv.service_uuids
    scanner.inject_advertisement(sensor_push_device, sensor_push_adv_2)

    data = scanner.discovered_devices_and_advertisement_data
    discovered_device, discovered_adv_data = data[sensor_push_device.address]
    assert discovered_device.address == sensor_push_device.address
    assert discovered_device.name == sensor_push_device.name
    assert discovered_adv_data.manufacturer_data == {
        **sensor_push_device_adv.manufacturer_data,
        **sensor_push_adv_2.manufacturer_data,
    }
    assert discovered_adv_data.service_data == {}
    assert set(discovered_adv_data.service_uuids) == {
        *sensor_push_device_adv.service_uuids
    }

    cancel()
    unsetup()


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_merge_manufacturer_data_history_new() -> None:
    """Test merging manufacturer data history."""
    manager = get_manager()

    sensor_push_device = generate_ble_device(
        "44:44:33:11:23:45",
        "",
        {},
        rssi=-60,
    )
    sensor_push_device_adv = generate_advertisement_data(
        local_name="",
        rssi=-60,
        manufacturer_data={
            64256: b"B\r.\xa9\xb6",
            31488: b"\x98\xfa\xb6\x91\xb6",
        },
        service_uuids=["ef090000-11d6-42ba-93b8-9dd7ec090ab0"],
        service_data={},
    )

    sensor_push_adv_2 = generate_advertisement_data(
        local_name="",
        service_uuids=["ef090000-11d6-42ba-93b8-9dd7ec090ab0"],
        service_data={},
        manufacturer_data={
            21248: b"\xb9\xe9\xe1\xb9\xb6",
        },
        rssi=-100,
    )

    connector = HaBluetoothConnector(
        MockBleakClient, "mock_bleak_client", lambda: False
    )
    scanner = FakeScanner("esp32", "esp32", connector, True)
    details = scanner.details
    assert details == HaScannerDetails(
        source=scanner.source,
        connectable=scanner.connectable,
        name=scanner.name,
        adapter=scanner.adapter,
    )
    unsetup = scanner.async_setup()
    cancel = manager.async_register_scanner(scanner)

    scanner.inject_advertisement(sensor_push_device, sensor_push_device_adv)

    data = scanner.discovered_devices_and_advertisement_data
    discovered_device, discovered_adv_data = data[sensor_push_device.address]
    assert discovered_device.address == sensor_push_device.address
    assert discovered_device.name == sensor_push_device.name
    assert (
        discovered_adv_data.manufacturer_data
        == sensor_push_device_adv.manufacturer_data
    )
    assert discovered_adv_data.service_data == sensor_push_device_adv.service_data
    assert discovered_adv_data.service_uuids == sensor_push_device_adv.service_uuids
    scanner.inject_advertisement(sensor_push_device, sensor_push_adv_2)

    data = scanner.discovered_devices_and_advertisement_data
    discovered_device, discovered_adv_data = data[sensor_push_device.address]
    assert discovered_device.address == sensor_push_device.address
    assert discovered_device.name == sensor_push_device.name
    assert discovered_adv_data.manufacturer_data == {
        **sensor_push_device_adv.manufacturer_data,
        **sensor_push_adv_2.manufacturer_data,
    }
    assert discovered_adv_data.service_data == {}
    assert set(discovered_adv_data.service_uuids) == {
        *sensor_push_device_adv.service_uuids
    }

    cancel()
    unsetup()
