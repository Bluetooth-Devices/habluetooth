from unittest.mock import ANY

import pytest
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak_retry_connector import BleakSlotManager
from bluetooth_adapters import BluetoothAdapters

from habluetooth import (
    BaseHaRemoteScanner,
    BaseHaScanner,
    BluetoothManager,
    BluetoothScanningMode,
    HaBluetoothConnector,
    HaScanner,
    set_manager,
)


@pytest.fixture(scope="session", autouse=True)
def manager():
    slot_manager = BleakSlotManager()
    bluetooth_adapters = BluetoothAdapters()
    set_manager(BluetoothManager(bluetooth_adapters, slot_manager))


class MockBleakClient:
    pass


def test_create_scanner():
    connector = HaBluetoothConnector(MockBleakClient, "any", lambda: True)

    class MockScanner(BaseHaScanner):
        pass

        def discovered_devices_and_advertisement_data(self):
            return []

        def discovered_devices(self):
            return []

    scanner = MockScanner("any", "any", connector)
    assert isinstance(scanner, BaseHaScanner)


def test_create_remote_scanner():
    connector = HaBluetoothConnector(MockBleakClient, "any", lambda: True)

    scanner = BaseHaRemoteScanner("any", "any", connector, True)
    assert isinstance(scanner, BaseHaRemoteScanner)


def test__async_on_advertisement():
    connector = HaBluetoothConnector(MockBleakClient, "any", lambda: True)

    scanner = BaseHaRemoteScanner("any", "any", connector, True)
    details = scanner._details | {}
    scanner._async_on_advertisement(
        "AA:BB:CC:DD:EE:FF",
        -88,
        "name",
        ["service_uuid"],
        {"service_uuid": b"\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b"},
        {32: b"\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b"},
        -88,
        details,
        1.0,
    )
    scanner._async_on_advertisement(
        "AA:BB:CC:DD:EE:FF",
        -21,
        "name",
        ["service_uuid2"],
        {"service_uuid2": b"\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b"},
        {21: b"\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b"},
        -88,
        details,
        1.0,
    )
    ble_device = BLEDevice(
        "AA:BB:CC:DD:EE:FF",
        "name",
        details,
        -21,
    )
    first_device = scanner.discovered_devices[0]
    assert first_device.address == ble_device.address
    assert first_device.details == ble_device.details
    assert first_device.name == ble_device.name
    assert first_device.rssi == ble_device.rssi
    assert "AA:BB:CC:DD:EE:FF" in scanner.discovered_devices_and_advertisement_data
    adv = scanner.discovered_devices_and_advertisement_data["AA:BB:CC:DD:EE:FF"][1]
    assert set(adv.service_data) == {"service_uuid", "service_uuid2"}
    assert adv == AdvertisementData(
        local_name="name",
        manufacturer_data={
            32: b"\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b",
            21: b"\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b",
        },
        service_data={
            "service_uuid": b"\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b",
            "service_uuid2": b"\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b",
        },
        service_uuids=ANY,
        tx_power=-88,
        rssi=-21,
        platform_data=(),
    )


def test_create_ha_scanner():
    scanner = HaScanner(BluetoothScanningMode.ACTIVE, "hci0", "AA:BB:CC:DD:EE:FF")
    assert isinstance(scanner, HaScanner)
