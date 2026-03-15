from unittest.mock import ANY, MagicMock

import pytest
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from habluetooth import (
    BaseHaRemoteScanner,
    BaseHaScanner,
    BluetoothScanningMode,
    HaBluetoothConnector,
    HaScanner,
    get_manager,
)
from habluetooth.models import ADV_DATA_UNKNOWN, BluetoothServiceInfoBleak


class MockBleakClient:
    pass


def test_create_scanner():
    connector = HaBluetoothConnector(MockBleakClient, "any", lambda: True)

    class MockScanner(BaseHaScanner):
        pass

        @property
        def discovered_devices_and_advertisement_data(self):
            return []

        @property
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
    )
    first_device = scanner.discovered_devices[0]
    assert first_device.address == ble_device.address
    assert first_device.details == ble_device.details
    assert first_device.name == ble_device.name
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
    assert len(scanner.discovered_devices) == 1
    assert scanner.discovered_devices[0].address == "AA:BB:CC:DD:EE:FF"
    assert len(scanner.discovered_devices_and_advertisement_data) == 1
    # BLEDevice no longer has rssi attribute in bleak 1.0+
    # rssi is only available in AdvertisementData
    assert (
        scanner.discovered_devices_and_advertisement_data["AA:BB:CC:DD:EE:FF"][1].rssi
        == -21
    )
    assert "AA:BB:CC:DD:EE:FF" in scanner.discovered_addresses
    device_adv = scanner.get_discovered_device_advertisement_data("AA:BB:CC:DD:EE:FF")
    assert device_adv is not None
    assert device_adv[1] == adv


def test__async_on_advertisement_first():
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
    device_adv = scanner.get_discovered_device_advertisement_data("AA:BB:CC:DD:EE:FF")
    assert device_adv is not None
    device, adv = device_adv
    assert device is not None
    assert adv is not None
    assert device.address == "AA:BB:CC:DD:EE:FF"
    assert adv.rssi == -88
    assert adv.service_uuids == ["service_uuid"]
    assert adv.service_data == {
        "service_uuid": b"\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b"
    }
    assert adv.manufacturer_data == {
        32: b"\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b"
    }
    assert adv.service_uuids == ANY
    assert adv.tx_power == -88
    assert adv.rssi == -88
    assert adv.platform_data == ()
    assert device.name == "name"
    assert device.details == details


def test__async_on_advertisement_prefers_longest_local_name():
    connector = HaBluetoothConnector(MockBleakClient, "any", lambda: True)

    scanner = BaseHaRemoteScanner("any", "any", connector, True)
    details = scanner._details | {}
    scanner._async_on_advertisement(
        "AA:BB:CC:DD:EE:FF",
        -88,
        "shortname",
        [],
        {},
        {},
        -88,
        details,
        1.0,
    )
    device_adv = scanner.get_discovered_device_advertisement_data("AA:BB:CC:DD:EE:FF")
    assert device_adv is not None
    device, adv = device_adv
    assert device is not None
    assert adv is not None
    assert device.name == "shortname"
    assert adv.local_name == "shortname"
    scanner._async_on_advertisement(
        "AA:BB:CC:DD:EE:FF",
        -88,
        "tinyname",
        [],
        {},
        {},
        -88,
        details,
        1.0,
    )
    device_adv = scanner.get_discovered_device_advertisement_data("AA:BB:CC:DD:EE:FF")
    assert device_adv is not None
    device, adv = device_adv
    assert device is not None
    assert adv is not None
    assert device.name == "shortname"
    assert adv.local_name == "shortname"
    scanner._async_on_advertisement(
        "AA:BB:CC:DD:EE:FF",
        -88,
        "longername",
        [],
        {},
        {},
        -88,
        details,
        1.0,
    )
    device_adv = scanner.get_discovered_device_advertisement_data("AA:BB:CC:DD:EE:FF")
    assert device_adv is not None
    device, adv = device_adv
    assert device is not None
    assert adv is not None
    assert device.name == "longername"
    assert adv.local_name == "longername"


def test_create_ha_scanner():
    scanner = HaScanner(BluetoothScanningMode.ACTIVE, "hci0", "AA:BB:CC:DD:EE:FF")
    assert isinstance(scanner, HaScanner)


@pytest.mark.asyncio
async def test_adv_data_unchanged_dedup_same_source():
    """Test that _adv_data_changed=UNCHANGED skips dedup when source matches."""
    manager = get_manager()
    connector = HaBluetoothConnector(MockBleakClient, "any", lambda: True)
    scanner = BaseHaRemoteScanner("source1", "source1", connector, True)
    cancel = manager.async_register_scanner(scanner)
    details = scanner._details | {}

    # First advertisement — seeds _all_history
    scanner._async_on_advertisement(
        "AA:BB:CC:DD:EE:FF",
        -88,
        "name",
        ["service_uuid"],
        {"service_uuid": b"\x01"},
        {1: b"\x01"},
        -88,
        details,
        1.0,
    )
    mock_discover = MagicMock()
    manager._subclass_discover_info = mock_discover

    # Second identical advertisement — _adv_data_changed will be UNCHANGED
    # and same source, so dedup should skip dispatch.
    # Use dynamically constructed strings to avoid CPython interning.
    scanner._async_on_advertisement(
        "".join(["AA:BB:CC:DD:EE:FF"]),
        -88,
        "".join(["name"]),
        ["".join(["service_uuid"])],
        {"".join(["service_uuid"]): b"\x01"},
        {1: b"\x01"},
        -88,
        details,
        2.0,
    )
    # Dedup should have returned early — _subclass_discover_info not called
    mock_discover.assert_not_called()

    cancel()


@pytest.mark.asyncio
async def test_adv_data_unchanged_different_source():
    """Test _adv_data_changed=UNCHANGED with different source falls through."""
    manager = get_manager()
    connector = HaBluetoothConnector(MockBleakClient, "any", lambda: True)

    scanner1 = BaseHaRemoteScanner("source1", "source1", connector, True)
    cancel1 = manager.async_register_scanner(scanner1)

    scanner2 = BaseHaRemoteScanner("source2", "source2", connector, True)
    cancel2 = manager.async_register_scanner(scanner2)

    details: dict[str, str] = {}

    # Scanner 1 sends advertisement — seeds _all_history with source1
    scanner1._async_on_advertisement(
        "AA:BB:CC:DD:EE:FF",
        -50,
        "name",
        ["svc"],
        {"svc": b"\x01"},
        {1: b"\x01"},
        -88,
        details,
        1.0,
    )

    # Scanner 2 sends first adv — seeds scanner2's _previous_service_info.
    # _all_history switches to source2 (stale time diff).
    scanner2._async_on_advertisement(
        "AA:BB:CC:DD:EE:FF",
        -40,
        "name",
        ["svc"],
        {"svc": b"\x01"},
        {1: b"\x01"},
        -88,
        details,
        1000.0,
    )
    assert manager._all_history["AA:BB:CC:DD:EE:FF"].source == "source2"

    # Scanner 1 sends again — seeds scanner1's _previous_service_info with
    # same data. _all_history now has source2.
    scanner1._async_on_advertisement(
        "AA:BB:CC:DD:EE:FF",
        -50,
        "name",
        ["svc"],
        {"svc": b"\x01"},
        {1: b"\x01"},
        -88,
        details,
        2001.0,
    )
    # _all_history switches back to source1 (stale time diff)
    assert manager._all_history["AA:BB:CC:DD:EE:FF"].source == "source1"

    mock_discover = MagicMock()
    manager._subclass_discover_info = mock_discover

    # Scanner 1 sends SAME data again — _adv_data_changed=UNCHANGED from
    # scanner1's perspective, but _all_history has source1 now, so
    # old_service_info.source IS service_info.source → dedup returns early.
    scanner1._async_on_advertisement(
        "AA:BB:CC:DD:EE:FF",
        -50,
        "name",
        ["svc"],
        {"svc": b"\x01"},
        {1: b"\x01"},
        -88,
        details,
        2002.0,
    )
    # Same source, unchanged — dedup should skip dispatch
    mock_discover.assert_not_called()

    # Now scanner 2 sends same data again — _adv_data_changed=UNCHANGED from
    # scanner2's perspective, but _all_history has source1.
    # old_service_info.source (source1) is NOT service_info.source (source2).
    # This covers line 704 being False.
    scanner2._async_on_advertisement(
        "AA:BB:CC:DD:EE:FF",
        -40,
        "name",
        ["svc"],
        {"svc": b"\x01"},
        {1: b"\x01"},
        -88,
        details,
        3001.0,
    )
    # Different source — should dispatch despite _adv_data_changed=UNCHANGED
    mock_discover.assert_called_once()
    assert manager._all_history["AA:BB:CC:DD:EE:FF"].source == "source2"

    cancel1()
    cancel2()


@pytest.mark.asyncio
async def test_adv_data_unknown_dedup():
    """Test that _adv_data_changed=UNKNOWN falls back to field comparison."""
    manager = get_manager()
    connector = HaBluetoothConnector(MockBleakClient, "any", lambda: True)
    scanner = BaseHaRemoteScanner("source1", "source1", connector, True)
    cancel = manager.async_register_scanner(scanner)

    device = BLEDevice("AA:BB:CC:DD:EE:FF", "name", {})
    mfr_data = {1: b"\x01"}
    svc_data = {"service_uuid": b"\x01"}
    svc_uuids = ["service_uuid"]

    # First advertisement — seeds _all_history
    info1 = BluetoothServiceInfoBleak(
        name="name",
        address="AA:BB:CC:DD:EE:FF",
        rssi=-88,
        manufacturer_data=mfr_data,
        service_data=svc_data,
        service_uuids=svc_uuids,
        source="source1",
        device=device,
        advertisement=None,
        connectable=True,
        time=1.0,
        tx_power=-88,
    )
    manager.scanner_adv_received(info1)

    # Second advertisement with same data but _adv_data_changed=UNKNOWN
    # (simulates Bleak/HaScanner path)
    info2 = BluetoothServiceInfoBleak(
        name="name",
        address="AA:BB:CC:DD:EE:FF",
        rssi=-88,
        manufacturer_data=mfr_data,
        service_data=svc_data,
        service_uuids=svc_uuids,
        source="source1",
        device=device,
        advertisement=None,
        connectable=True,
        time=2.0,
        tx_power=-88,
    )
    info2._adv_data_changed = ADV_DATA_UNKNOWN

    mock_discover = MagicMock()
    manager._subclass_discover_info = mock_discover

    manager.scanner_adv_received(info2)

    # Same data with ADV_DATA_UNKNOWN — field comparison should detect
    # no change and dedup (return early)
    mock_discover.assert_not_called()

    cancel()
