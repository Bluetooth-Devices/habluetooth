"""Tests for the Bluetooth base scanner models."""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from typing import Any
from unittest.mock import ANY, MagicMock

import pytest
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bluetooth_data_tools import monotonic_time_coarse

from habluetooth import (
    BaseHaRemoteScanner,
    BaseHaScanner,
    BluetoothScanningMode,
    HaBluetoothConnector,
    HaScannerDetails,
    HaScannerModeChange,
    HaScannerType,
    get_manager,
)
from habluetooth.const import (
    CONNECTABLE_FALLBACK_MAXIMUM_STALE_ADVERTISEMENT_SECONDS,
    FALLBACK_MAXIMUM_STALE_ADVERTISEMENT_SECONDS,
    SCANNER_WATCHDOG_INTERVAL,
    SCANNER_WATCHDOG_TIMEOUT,
)
from habluetooth.storage import (
    DiscoveredDeviceAdvertisementData,
)

from . import (
    HCI0_SOURCE_ADDRESS,
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

    def inject_raw_advertisement(
        self,
        address: str,
        rssi: int,
        adv: bytes,
        now: float | None = None,
    ) -> None:
        """Inject a raw advertisement."""
        self._async_on_raw_advertisement(
            address,
            rssi,
            adv,
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
        scanner_type=HaScannerType.REMOTE,
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

    assert (
        "00090401-0052-036b-3206-ff0a050a021a"
        not in scanner.discovered_devices_and_advertisement_data[
            switchbot_device_2.address
        ][1].service_data
    )

    scanner.inject_raw_advertisement(
        switchbot_device_2.address,
        0,
        b"\x12\x21\x1a\x02\n\x05\n\xff\x062k\x03R\x00\x01\x04\t\x00\x04",
    )

    assert (
        "00090401-0052-036b-3206-ff0a050a021a"
        in scanner.discovered_devices_and_advertisement_data[
            switchbot_device_2.address
        ][1].service_data
    )

    assert scanner.serialize_discovered_devices() == DiscoveredDeviceAdvertisementData(
        connectable=True,
        expire_seconds=195,
        discovered_device_advertisement_datas={"44:44:33:11:23:45": ANY},
        discovered_device_timestamps={"44:44:33:11:23:45": ANY},
        discovered_device_raw={
            "44:44:33:11:23:45": b"\x12!\x1a\x02"
            b"\n\x05\n\xff"
            b"\x062k\x03"
            b"R\x00\x01\x04"
            b"\t\x00\x04"
        },
    )

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
        scanner_type=HaScannerType.REMOTE,
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
        scanner_type=HaScannerType.REMOTE,
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
async def test_filter_apple_data() -> None:
    """Test filtering apple data accepts bytes that start with 01."""
    manager = get_manager()

    device = generate_ble_device(
        "44:44:33:11:23:45",
        "",
        {},
        rssi=-60,
    )
    device_adv = generate_advertisement_data(
        local_name="",
        rssi=-60,
        manufacturer_data={
            76: b"\x01\r.\xa9\xb6",
        },
        service_uuids=["ef090000-11d6-42ba-93b8-9dd7ec090ab0"],
        service_data={},
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
        scanner_type=HaScannerType.REMOTE,
    )
    unsetup = scanner.async_setup()
    cancel = manager.async_register_scanner(scanner)

    scanner.inject_advertisement(device, device_adv)

    data = scanner.discovered_devices_and_advertisement_data
    discovered_device, discovered_adv_data = data[device.address]
    assert discovered_device.address == device.address
    assert discovered_device.name == device.name
    assert discovered_adv_data.manufacturer_data == device_adv.manufacturer_data
    unsetup()
    cancel()


@pytest.mark.usefixtures("register_hci0_scanner")
def test_connection_history_count_in_progress() -> None:
    """Test connection history in process counting."""
    manager = get_manager()
    device1_address = "44:44:33:11:23:12"
    device2_address = "44:44:33:11:23:13"
    hci0_scanner = manager.async_scanner_by_source(HCI0_SOURCE_ADDRESS)
    assert hci0_scanner is not None
    hci0_scanner._add_connecting(device1_address)
    assert hci0_scanner._connections_in_progress() == 1
    hci0_scanner._add_connecting(device1_address)
    hci0_scanner._add_connecting(device2_address)
    assert hci0_scanner._connections_in_progress() == 3
    hci0_scanner._finished_connecting(device1_address, True)
    assert hci0_scanner._connections_in_progress() == 2
    hci0_scanner._finished_connecting(device1_address, False)
    assert hci0_scanner._connections_in_progress() == 1
    hci0_scanner._finished_connecting(device2_address, False)
    assert hci0_scanner._connections_in_progress() == 0


@pytest.mark.usefixtures("register_hci0_scanner")
def test_connection_history_failure_count(caplog: pytest.LogCaptureFixture) -> None:
    """Test connection history failure count."""
    manager = get_manager()
    device1_address = "44:44:33:11:23:12"
    device2_address = "44:44:33:11:23:13"
    hci0_scanner = manager.async_scanner_by_source(HCI0_SOURCE_ADDRESS)
    assert hci0_scanner is not None
    hci0_scanner._add_connecting(device1_address)
    hci0_scanner._finished_connecting(device1_address, False)
    assert hci0_scanner._connection_failures(device1_address) == 1
    hci0_scanner._add_connecting(device1_address)
    hci0_scanner._add_connecting(device2_address)
    hci0_scanner._finished_connecting(device1_address, False)
    assert hci0_scanner._connection_failures(device1_address) == 2
    hci0_scanner._finished_connecting(device2_address, False)
    assert hci0_scanner._connection_failures(device2_address) == 1
    hci0_scanner._add_connecting(device1_address)
    hci0_scanner._finished_connecting(device1_address, True)
    # On success, we should reset the failure count
    assert hci0_scanner._connection_failures(device1_address) == 0

    assert "Removing a non-existing connecting" not in caplog.text
    hci0_scanner._finished_connecting(device1_address, True)
    assert "Removing a non-existing connecting" in caplog.text


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_scanner_mode_changes() -> None:
    """Test scanner mode change methods notify the manager."""
    manager = get_manager()

    # Track mode changes
    mode_changes: list[HaScannerModeChange] = []

    def mode_callback(change: HaScannerModeChange) -> None:
        """Track mode changes."""
        mode_changes.append(change)

    cancel = manager.async_register_scanner_mode_change_callback(mode_callback, None)

    # Create a scanner with initial modes
    scanner = FakeScanner(
        HCI0_SOURCE_ADDRESS,
        "hci0",
        connectable=True,
        requested_mode=BluetoothScanningMode.PASSIVE,
        current_mode=BluetoothScanningMode.PASSIVE,
    )

    # Set up the scanner
    unsetup = scanner.async_setup()

    # Test changing requested mode
    scanner.set_requested_mode(BluetoothScanningMode.ACTIVE)
    assert len(mode_changes) == 1
    assert mode_changes[0].scanner == scanner
    assert mode_changes[0].requested_mode == BluetoothScanningMode.ACTIVE
    assert mode_changes[0].current_mode == BluetoothScanningMode.PASSIVE
    assert scanner.requested_mode == BluetoothScanningMode.ACTIVE

    # Test changing current mode
    scanner.set_current_mode(BluetoothScanningMode.ACTIVE)
    assert len(mode_changes) == 2
    assert mode_changes[1].scanner == scanner
    assert mode_changes[1].requested_mode == BluetoothScanningMode.ACTIVE
    assert mode_changes[1].current_mode == BluetoothScanningMode.ACTIVE
    assert scanner.current_mode == BluetoothScanningMode.ACTIVE

    # Test no notification when mode doesn't change
    scanner.set_current_mode(BluetoothScanningMode.ACTIVE)
    assert len(mode_changes) == 2  # No new notification

    # Test setting to None
    scanner.set_requested_mode(None)
    assert len(mode_changes) == 3
    assert mode_changes[2].requested_mode is None
    assert scanner.requested_mode is None

    scanner.set_current_mode(None)  # type: ignore[unreachable]
    assert len(mode_changes) == 4
    assert mode_changes[3].current_mode is None
    assert scanner.current_mode is None

    # Clean up
    unsetup()
    cancel()


def test_remote_scanner_type() -> None:
    """Test that remote scanners have REMOTE type."""

    class TestRemoteScanner(BaseHaRemoteScanner):
        """Test remote scanner implementation."""

        pass

    scanner = TestRemoteScanner("test_source", "test_adapter")
    assert scanner.details.scanner_type is HaScannerType.REMOTE


def test_base_scanner_with_connector() -> None:
    """Test BaseHaScanner with connector and adapter type."""
    manager = get_manager()

    mock_adapters: dict[str, dict[str, Any]] = {
        "test_adapter": {
            "address": "00:1A:7D:DA:71:04",
            "adapter_type": "usb",
        }
    }

    connector = HaBluetoothConnector(
        client=MagicMock, source="test_source", can_connect=lambda: True
    )

    original_adapters = manager._adapters
    manager._adapters = mock_adapters
    try:
        scanner = BaseHaScanner(
            source="test_source",
            adapter="test_adapter",
            connector=connector,
            connectable=True,
        )
        assert scanner.details.scanner_type is HaScannerType.USB
    finally:
        manager._adapters = original_adapters
