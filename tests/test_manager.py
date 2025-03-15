"""Tests for the manager."""

import asyncio
import time
from datetime import timedelta
from typing import Any
from unittest.mock import ANY, patch

import pytest
from bleak_retry_connector import AllocationChange, Allocations, BleakSlotManager
from bluetooth_adapters.systems.linux import LinuxAdapters
from freezegun import freeze_time

from habluetooth import (
    FALLBACK_MAXIMUM_STALE_ADVERTISEMENT_SECONDS,
    TRACKER_BUFFERING_WOBBLE_SECONDS,
    UNAVAILABLE_TRACK_SECONDS,
    BluetoothManager,
    BluetoothServiceInfoBleak,
    HaBluetoothSlotAllocations,
    HaScannerRegistration,
    HaScannerRegistrationEvent,
    get_manager,
    set_manager,
)

from . import (
    HCI0_SOURCE_ADDRESS,
    HCI1_SOURCE_ADDRESS,
    async_fire_time_changed,
    generate_advertisement_data,
    generate_ble_device,
    inject_advertisement_with_source,
    inject_advertisement_with_time_and_source,
    inject_advertisement_with_time_and_source_connectable,
    patch_bluetooth_time,
    utcnow,
)
from .conftest import FakeBluetoothAdapters, FakeScanner

SOURCE_LOCAL = "local"


@pytest.mark.asyncio
@pytest.mark.skipif("platform.system() == 'Windows'")
async def test_async_recover_failed_adapters() -> None:
    """Return the BluetoothManager instance."""
    attempt = 0

    class MockLinuxAdapters(LinuxAdapters):
        @property
        def adapters(self) -> dict[str, Any]:
            nonlocal attempt
            attempt += 1

            if attempt == 1:
                return {
                    "hci0": {
                        "address": "00:00:00:00:00:01",
                        "hw_version": "usb:v1D6Bp0246d053F",
                        "passive_scan": False,
                        "sw_version": "homeassistant",
                        "manufacturer": "ACME",
                        "product": "Bluetooth Adapter 5.0",
                        "product_id": "aa01",
                        "vendor_id": "cc01",
                    },
                    "hci1": {
                        "address": "00:00:00:00:00:00",
                        "hw_version": "usb:v1D6Bp0246d053F",
                        "passive_scan": False,
                        "sw_version": "homeassistant",
                        "manufacturer": "ACME",
                        "product": "Bluetooth Adapter 5.0",
                        "product_id": "aa01",
                        "vendor_id": "cc01",
                    },
                    "hci2": {
                        "address": "00:00:00:00:00:00",
                        "hw_version": "usb:v1D6Bp0246d053F",
                        "passive_scan": False,
                        "sw_version": "homeassistant",
                        "manufacturer": "ACME",
                        "product": "Bluetooth Adapter 5.0",
                        "product_id": "aa01",
                        "vendor_id": "cc01",
                    },
                }

            return {
                "hci0": {
                    "address": "00:00:00:00:00:01",
                    "hw_version": "usb:v1D6Bp0246d053F",
                    "passive_scan": False,
                    "sw_version": "homeassistant",
                    "manufacturer": "ACME",
                    "product": "Bluetooth Adapter 5.0",
                    "product_id": "aa01",
                    "vendor_id": "cc01",
                },
                "hci1": {
                    "address": "00:00:00:00:00:02",
                    "hw_version": "usb:v1D6Bp0246d053F",
                    "passive_scan": False,
                    "sw_version": "homeassistant",
                    "manufacturer": "ACME",
                    "product": "Bluetooth Adapter 5.0",
                    "product_id": "aa01",
                    "vendor_id": "cc01",
                },
                "hci2": {
                    "address": "00:00:00:00:00:03",
                    "hw_version": "usb:v1D6Bp0246d053F",
                    "passive_scan": False,
                    "sw_version": "homeassistant",
                    "manufacturer": "ACME",
                    "product": "Bluetooth Adapter 5.0",
                    "product_id": "aa01",
                    "vendor_id": "cc01",
                },
            }

    with (
        patch("habluetooth.manager.async_reset_adapter") as mock_async_reset_adapter,
    ):
        adapters = MockLinuxAdapters()
        slot_manager = BleakSlotManager()
        manager = BluetoothManager(adapters, slot_manager)
        await manager.async_setup()
        set_manager(manager)
        adapter = await manager.async_get_adapter_from_address_or_recover(
            "00:00:00:00:00:03"
        )
        assert adapter == "hci2"
        adapter = await manager.async_get_adapter_from_address_or_recover(
            "00:00:00:00:00:02"
        )
        assert adapter == "hci1"
        adapter = await manager.async_get_adapter_from_address_or_recover(
            "00:00:00:00:00:01"
        )
        assert adapter == "hci0"

    assert mock_async_reset_adapter.call_count == 2
    assert mock_async_reset_adapter.call_args_list == [
        (("hci1", "00:00:00:00:00:00"),),
        (("hci2", "00:00:00:00:00:00"),),
    ]


@pytest.mark.asyncio
async def test_create_manager() -> None:
    """Return the BluetoothManager instance."""
    adapters = FakeBluetoothAdapters()
    slot_manager = BleakSlotManager()
    manager = BluetoothManager(adapters, slot_manager)
    set_manager(manager)
    assert manager


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_bluetooth")
async def test_async_register_disappeared_callback(
    register_hci0_scanner: None,
    register_hci1_scanner: None,
) -> None:
    """Test bluetooth async_register_disappeared_callback handles failures."""
    manager = get_manager()
    assert manager._loop is not None

    address = "44:44:33:11:23:12"

    switchbot_device_signal_100 = generate_ble_device(
        address, "wohand_signal_100", rssi=-100
    )
    switchbot_adv_signal_100 = generate_advertisement_data(
        local_name="wohand_signal_100", service_uuids=[]
    )
    inject_advertisement_with_source(
        switchbot_device_signal_100, switchbot_adv_signal_100, "hci0"
    )

    failed_disappeared: list[str] = []

    def _failing_callback(_address: str) -> None:
        """Failing callback."""
        failed_disappeared.append(_address)
        raise ValueError("This is a test")

    ok_disappeared: list[str] = []

    def _ok_callback(_address: str) -> None:
        """Ok callback."""
        ok_disappeared.append(_address)

    cancel1 = manager.async_register_disappeared_callback(_failing_callback)
    # Make sure the second callback still works if the first one fails and
    # raises an exception
    cancel2 = manager.async_register_disappeared_callback(_ok_callback)

    switchbot_adv_signal_100 = generate_advertisement_data(
        local_name="wohand_signal_100",
        manufacturer_data={123: b"abc"},
        service_uuids=[],
        rssi=-80,
    )
    inject_advertisement_with_source(
        switchbot_device_signal_100, switchbot_adv_signal_100, "hci1"
    )

    future_time = utcnow() + timedelta(seconds=3600)
    future_monotonic_time = time.monotonic() + 3600
    with (
        freeze_time(future_time),
        patch(
            "habluetooth.manager.monotonic_time_coarse",
            return_value=future_monotonic_time,
        ),
    ):
        manager._async_check_unavailable()
        async_fire_time_changed(future_time)

    assert len(ok_disappeared) == 1
    assert ok_disappeared[0] == address
    assert len(failed_disappeared) == 1
    assert failed_disappeared[0] == address

    cancel1()
    cancel2()


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_bluetooth")
async def test_async_register_allocation_callback(
    register_hci0_scanner: None,
    register_hci1_scanner: None,
) -> None:
    """Test bluetooth async_register_allocation_callback handles failures."""
    manager = get_manager()
    assert manager._loop is not None

    address = "44:44:33:11:23:12"

    switchbot_device_signal_100 = generate_ble_device(
        address, "wohand_signal_100", rssi=-100
    )
    switchbot_adv_signal_100 = generate_advertisement_data(
        local_name="wohand_signal_100", service_uuids=[]
    )
    inject_advertisement_with_source(
        switchbot_device_signal_100, switchbot_adv_signal_100, "hci0"
    )

    failed_allocations: list[HaBluetoothSlotAllocations] = []

    def _failing_callback(allocations: HaBluetoothSlotAllocations) -> None:
        """Failing callback."""
        failed_allocations.append(allocations)
        raise ValueError("This is a test")

    ok_allocations: list[HaBluetoothSlotAllocations] = []

    def _ok_callback(allocations: HaBluetoothSlotAllocations) -> None:
        """Ok callback."""
        ok_allocations.append(allocations)

    cancel1 = manager.async_register_allocation_callback(_failing_callback)
    # Make sure the second callback still works if the first one fails and
    # raises an exception
    cancel2 = manager.async_register_allocation_callback(_ok_callback)

    switchbot_adv_signal_100 = generate_advertisement_data(
        local_name="wohand_signal_100",
        manufacturer_data={123: b"abc"},
        service_uuids=[],
        rssi=-80,
    )
    inject_advertisement_with_source(
        switchbot_device_signal_100, switchbot_adv_signal_100, "hci1"
    )

    assert manager.async_current_allocations() == [
        HaBluetoothSlotAllocations(
            source="AA:BB:CC:DD:EE:00", slots=5, free=5, allocated=[]
        ),
        HaBluetoothSlotAllocations(
            source="AA:BB:CC:DD:EE:11", slots=5, free=5, allocated=[]
        ),
    ]
    manager.async_on_allocation_changed(
        Allocations(
            "AA:BB:CC:DD:EE:00",
            5,
            4,
            ["44:44:33:11:23:12"],
        )
    )

    assert len(ok_allocations) == 1
    assert ok_allocations[0] == HaBluetoothSlotAllocations(
        "AA:BB:CC:DD:EE:00",
        5,
        4,
        ["44:44:33:11:23:12"],
    )
    assert len(failed_allocations) == 1
    assert failed_allocations[0] == HaBluetoothSlotAllocations(
        "AA:BB:CC:DD:EE:00",
        5,
        4,
        ["44:44:33:11:23:12"],
    )

    with patch.object(
        manager.slot_manager,
        "get_allocations",
        return_value=Allocations(
            adapter="hci0",
            slots=5,
            free=4,
            allocated=["44:44:33:11:23:12"],
        ),
    ):
        manager.slot_manager._call_callbacks(
            AllocationChange.ALLOCATED, "/org/bluez/hci0/dev_44_44_33_11_23_12"
        )

    assert len(ok_allocations) == 2

    assert manager.async_current_allocations() == [
        HaBluetoothSlotAllocations("AA:BB:CC:DD:EE:00", 5, 4, ["44:44:33:11:23:12"]),
        HaBluetoothSlotAllocations(
            source="AA:BB:CC:DD:EE:11", slots=5, free=5, allocated=[]
        ),
    ]
    assert manager.async_current_allocations("AA:BB:CC:DD:EE:00") == [
        HaBluetoothSlotAllocations("AA:BB:CC:DD:EE:00", 5, 4, ["44:44:33:11:23:12"]),
    ]
    cancel1()
    cancel2()


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_bluetooth")
async def test_async_register_allocation_callback_non_connectable(
    register_non_connectable_scanner: None,
) -> None:
    """Test async_current_allocations for a non-connectable scanner."""
    manager = get_manager()
    assert manager._loop is not None
    assert manager.async_current_allocations() == [
        HaBluetoothSlotAllocations(
            source="AA:BB:CC:DD:EE:FF",
            slots=0,
            free=0,
            allocated=[],
        ),
    ]


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_bluetooth")
async def test_async_register_scanner_registration_callback(
    register_hci0_scanner: None,
    register_hci1_scanner: None,
) -> None:
    """Test bluetooth async_register_scanner_registration_callback handles failures."""
    manager = get_manager()
    assert manager._loop is not None

    scanners = manager.async_current_scanners()
    assert len(scanners) == 2
    sources = {scanner.source for scanner in scanners}
    assert sources == {"AA:BB:CC:DD:EE:00", "AA:BB:CC:DD:EE:11"}

    failed_scanner_callbacks: list[HaScannerRegistration] = []

    def _failing_callback(scanner_registration: HaScannerRegistration) -> None:
        """Failing callback."""
        failed_scanner_callbacks.append(scanner_registration)
        raise ValueError("This is a test")

    ok_scanner_callbacks: list[HaScannerRegistration] = []

    def _ok_callback(scanner_registration: HaScannerRegistration) -> None:
        """Ok callback."""
        ok_scanner_callbacks.append(scanner_registration)

    cancel1 = manager.async_register_scanner_registration_callback(
        _failing_callback, None
    )
    # Make sure the second callback still works if the first one fails and
    # raises an exception
    cancel2 = manager.async_register_scanner_registration_callback(_ok_callback, None)

    hci3_scanner = FakeScanner("AA:BB:CC:DD:EE:33", "hci3")
    hci3_scanner.connectable = True
    manager = get_manager()
    cancel = manager.async_register_scanner(hci3_scanner, connection_slots=5)

    assert len(ok_scanner_callbacks) == 1
    assert ok_scanner_callbacks[0] == HaScannerRegistration(
        HaScannerRegistrationEvent.ADDED, hci3_scanner
    )
    assert len(failed_scanner_callbacks) == 1

    cancel()

    assert len(ok_scanner_callbacks) == 2
    assert ok_scanner_callbacks[1] == HaScannerRegistration(
        HaScannerRegistrationEvent.REMOVED, hci3_scanner
    )
    cancel1()
    cancel2()


@pytest.mark.asyncio
async def test_async_register_scanner_with_connection_slots() -> None:
    """Test registering a scanner with connection slots."""
    manager = get_manager()
    assert manager._loop is not None

    scanners = manager.async_current_scanners()
    assert len(scanners) == 0

    hci3_scanner = FakeScanner("AA:BB:CC:DD:EE:33", "hci3")
    hci3_scanner.connectable = True
    manager = get_manager()
    cancel = manager.async_register_scanner(hci3_scanner, connection_slots=5)
    assert manager.async_current_allocations(hci3_scanner.source) == [
        HaBluetoothSlotAllocations(hci3_scanner.source, 5, 5, [])
    ]

    cancel()


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_bluetooth")
async def test_diagnostics(register_hci0_scanner: None) -> None:
    """Test bluetooth diagnostics."""
    manager = get_manager()
    assert manager._loop is not None
    manager.async_on_allocation_changed(
        Allocations(
            "AA:BB:CC:DD:EE:00",
            5,
            4,
            ["44:44:33:11:23:12"],
        )
    )
    diagnostics = await manager.async_diagnostics()
    assert diagnostics == {
        "adapters": {},
        "advertisement_tracker": ANY,
        "all_history": ANY,
        "allocations": {
            "AA:BB:CC:DD:EE:00": {
                "allocated": ["44:44:33:11:23:12"],
                "free": 4,
                "slots": 5,
                "source": "AA:BB:CC:DD:EE:00",
            }
        },
        "connectable_history": ANY,
        "scanners": [
            {
                "discovered_devices_and_advertisement_data": [],
                "connectable": True,
                "current_mode": None,
                "requested_mode": None,
                "last_detection": 0.0,
                "monotonic_time": ANY,
                "name": "hci0 (AA:BB:CC:DD:EE:00)",
                "scanning": True,
                "source": "AA:BB:CC:DD:EE:00",
                "start_time": 0.0,
                "type": "FakeScanner",
            }
        ],
        "slot_manager": {
            "adapter_slots": {"hci0": 5},
            "allocations_by_adapter": {"hci0": []},
            "manager": False,
        },
    }


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_advertisements_do_not_switch_adapters_for_no_reason(
    register_hci0_scanner: None,
    register_hci1_scanner: None,
) -> None:
    """Test we only switch adapters when needed."""
    address = "44:44:33:11:23:12"

    switchbot_device_signal_100 = generate_ble_device(
        address, "wohand_signal_100", rssi=-100
    )
    switchbot_adv_signal_100 = generate_advertisement_data(
        local_name="wohand_signal_100", service_uuids=[]
    )
    inject_advertisement_with_source(
        switchbot_device_signal_100, switchbot_adv_signal_100, HCI0_SOURCE_ADDRESS
    )

    assert (
        get_manager().async_ble_device_from_address(address, True)
        is switchbot_device_signal_100
    )

    switchbot_device_signal_99 = generate_ble_device(
        address, "wohand_signal_99", rssi=-99
    )
    switchbot_adv_signal_99 = generate_advertisement_data(
        local_name="wohand_signal_99", service_uuids=[]
    )
    inject_advertisement_with_source(
        switchbot_device_signal_99, switchbot_adv_signal_99, HCI0_SOURCE_ADDRESS
    )

    assert (
        get_manager().async_ble_device_from_address(address, True)
        is switchbot_device_signal_99
    )

    switchbot_device_signal_98 = generate_ble_device(
        address, "wohand_good_signal", rssi=-98
    )
    switchbot_adv_signal_98 = generate_advertisement_data(
        local_name="wohand_good_signal", service_uuids=[]
    )
    inject_advertisement_with_source(
        switchbot_device_signal_98, switchbot_adv_signal_98, HCI1_SOURCE_ADDRESS
    )

    # should not switch to hci1
    assert (
        get_manager().async_ble_device_from_address(address, True)
        is switchbot_device_signal_99
    )


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_switching_adapters_based_on_rssi(
    register_hci0_scanner: None,
    register_hci1_scanner: None,
) -> None:
    """Test switching adapters based on rssi."""
    address = "44:44:33:11:23:45"

    switchbot_device_poor_signal = generate_ble_device(address, "wohand_poor_signal")
    switchbot_adv_poor_signal = generate_advertisement_data(
        local_name="wohand_poor_signal", service_uuids=[], rssi=-100
    )
    inject_advertisement_with_source(
        switchbot_device_poor_signal,
        switchbot_adv_poor_signal,
        HCI0_SOURCE_ADDRESS,
    )

    assert (
        get_manager().async_ble_device_from_address(address, True)
        is switchbot_device_poor_signal
    )

    switchbot_device_good_signal = generate_ble_device(address, "wohand_good_signal")
    switchbot_adv_good_signal = generate_advertisement_data(
        local_name="wohand_good_signal", service_uuids=[], rssi=-60
    )
    inject_advertisement_with_source(
        switchbot_device_good_signal,
        switchbot_adv_good_signal,
        HCI1_SOURCE_ADDRESS,
    )

    assert (
        get_manager().async_ble_device_from_address(address, True)
        is switchbot_device_good_signal
    )

    inject_advertisement_with_source(
        switchbot_device_good_signal,
        switchbot_adv_poor_signal,
        HCI0_SOURCE_ADDRESS,
    )
    assert (
        get_manager().async_ble_device_from_address(address, True)
        is switchbot_device_good_signal
    )

    # We should not switch adapters unless the signal hits the threshold
    switchbot_device_similar_signal = generate_ble_device(
        address, "wohand_similar_signal"
    )
    switchbot_adv_similar_signal = generate_advertisement_data(
        local_name="wohand_similar_signal", service_uuids=[], rssi=-62
    )

    inject_advertisement_with_source(
        switchbot_device_similar_signal,
        switchbot_adv_similar_signal,
        HCI0_SOURCE_ADDRESS,
    )
    assert (
        get_manager().async_ble_device_from_address(address, True)
        is switchbot_device_good_signal
    )


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_switching_adapters_based_on_zero_rssi(
    register_hci0_scanner: None,
    register_hci1_scanner: None,
) -> None:
    """Test switching adapters based on zero rssi."""
    address = "44:44:33:11:23:45"

    switchbot_device_no_rssi = generate_ble_device(address, "wohand_poor_signal")
    switchbot_adv_no_rssi = generate_advertisement_data(
        local_name="wohand_no_rssi", service_uuids=[], rssi=0
    )
    inject_advertisement_with_source(
        switchbot_device_no_rssi, switchbot_adv_no_rssi, HCI0_SOURCE_ADDRESS
    )

    assert (
        get_manager().async_ble_device_from_address(address, True)
        is switchbot_device_no_rssi
    )

    switchbot_device_good_signal = generate_ble_device(address, "wohand_good_signal")
    switchbot_adv_good_signal = generate_advertisement_data(
        local_name="wohand_good_signal", service_uuids=[], rssi=-60
    )
    inject_advertisement_with_source(
        switchbot_device_good_signal,
        switchbot_adv_good_signal,
        HCI1_SOURCE_ADDRESS,
    )

    assert (
        get_manager().async_ble_device_from_address(address, True)
        is switchbot_device_good_signal
    )

    inject_advertisement_with_source(
        switchbot_device_good_signal, switchbot_adv_no_rssi, HCI0_SOURCE_ADDRESS
    )
    assert (
        get_manager().async_ble_device_from_address(address, True)
        is switchbot_device_good_signal
    )

    # We should not switch adapters unless the signal hits the threshold
    switchbot_device_similar_signal = generate_ble_device(
        address, "wohand_similar_signal"
    )
    switchbot_adv_similar_signal = generate_advertisement_data(
        local_name="wohand_similar_signal", service_uuids=[], rssi=-62
    )

    inject_advertisement_with_source(
        switchbot_device_similar_signal,
        switchbot_adv_similar_signal,
        HCI0_SOURCE_ADDRESS,
    )
    assert (
        get_manager().async_ble_device_from_address(address, True)
        is switchbot_device_good_signal
    )


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_switching_adapters_based_on_stale(
    register_hci0_scanner: None,
    register_hci1_scanner: None,
) -> None:
    """Test switching adapters based on the previous advertisement being stale."""
    address = "44:44:33:11:23:41"
    start_time_monotonic = 50.0

    switchbot_device_poor_signal_hci0 = generate_ble_device(
        address, "wohand_poor_signal_hci0"
    )
    switchbot_adv_poor_signal_hci0 = generate_advertisement_data(
        local_name="wohand_poor_signal_hci0", service_uuids=[], rssi=-100
    )
    inject_advertisement_with_time_and_source(
        switchbot_device_poor_signal_hci0,
        switchbot_adv_poor_signal_hci0,
        start_time_monotonic,
        HCI0_SOURCE_ADDRESS,
    )

    assert (
        get_manager().async_ble_device_from_address(address, True)
        is switchbot_device_poor_signal_hci0
    )

    switchbot_device_poor_signal_hci1 = generate_ble_device(
        address, "wohand_poor_signal_hci1"
    )
    switchbot_adv_poor_signal_hci1 = generate_advertisement_data(
        local_name="wohand_poor_signal_hci1", service_uuids=[], rssi=-99
    )
    inject_advertisement_with_time_and_source(
        switchbot_device_poor_signal_hci1,
        switchbot_adv_poor_signal_hci1,
        start_time_monotonic,
        HCI1_SOURCE_ADDRESS,
    )

    # Should not switch adapters until the advertisement is stale
    assert (
        get_manager().async_ble_device_from_address(address, True)
        is switchbot_device_poor_signal_hci0
    )

    # Should switch to hci1 since the previous advertisement is stale
    # even though the signal is poor because the device is now
    # likely unreachable via hci0
    inject_advertisement_with_time_and_source(
        switchbot_device_poor_signal_hci1,
        switchbot_adv_poor_signal_hci1,
        start_time_monotonic + FALLBACK_MAXIMUM_STALE_ADVERTISEMENT_SECONDS + 1,
        "hci1",
    )

    assert (
        get_manager().async_ble_device_from_address(address, True)
        is switchbot_device_poor_signal_hci1
    )


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_switching_adapters_based_on_stale_with_discovered_interval(
    register_hci0_scanner: None,
    register_hci1_scanner: None,
) -> None:
    """Test switching with discovered interval."""
    address = "44:44:33:11:23:41"
    start_time_monotonic = 50.0

    switchbot_device_poor_signal_hci0 = generate_ble_device(
        address, "wohand_poor_signal_hci0"
    )
    switchbot_adv_poor_signal_hci0 = generate_advertisement_data(
        local_name="wohand_poor_signal_hci0", service_uuids=[], rssi=-100
    )
    inject_advertisement_with_time_and_source(
        switchbot_device_poor_signal_hci0,
        switchbot_adv_poor_signal_hci0,
        start_time_monotonic,
        HCI0_SOURCE_ADDRESS,
    )

    assert (
        get_manager().async_ble_device_from_address(address, True)
        is switchbot_device_poor_signal_hci0
    )

    get_manager().async_set_fallback_availability_interval(address, 10)

    switchbot_device_poor_signal_hci1 = generate_ble_device(
        address, "wohand_poor_signal_hci1"
    )
    switchbot_adv_poor_signal_hci1 = generate_advertisement_data(
        local_name="wohand_poor_signal_hci1", service_uuids=[], rssi=-99
    )
    inject_advertisement_with_time_and_source(
        switchbot_device_poor_signal_hci1,
        switchbot_adv_poor_signal_hci1,
        start_time_monotonic,
        HCI1_SOURCE_ADDRESS,
    )

    # Should not switch adapters until the advertisement is stale
    assert (
        get_manager().async_ble_device_from_address(address, True)
        is switchbot_device_poor_signal_hci0
    )

    inject_advertisement_with_time_and_source(
        switchbot_device_poor_signal_hci1,
        switchbot_adv_poor_signal_hci1,
        start_time_monotonic + 10 + 1,
        HCI1_SOURCE_ADDRESS,
    )

    # Should not switch yet since we are not within the
    # wobble period
    assert (
        get_manager().async_ble_device_from_address(address, True)
        is switchbot_device_poor_signal_hci0
    )

    inject_advertisement_with_time_and_source(
        switchbot_device_poor_signal_hci1,
        switchbot_adv_poor_signal_hci1,
        start_time_monotonic + 10 + TRACKER_BUFFERING_WOBBLE_SECONDS + 1,
        HCI1_SOURCE_ADDRESS,
    )
    # Should switch to hci1 since the previous advertisement is stale
    # even though the signal is poor because the device is now
    # likely unreachable via hci0
    assert (
        get_manager().async_ble_device_from_address(address, True)
        is switchbot_device_poor_signal_hci1
    )


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_switching_adapters_based_on_rssi_connectable_to_non_connectable(
    register_hci0_scanner: None,
    register_hci1_scanner: None,
) -> None:
    """Test switching adapters based on rssi from connectable to non connectable."""
    address = "44:44:33:11:23:45"
    now = time.monotonic()
    switchbot_device_poor_signal = generate_ble_device(address, "wohand_poor_signal")
    switchbot_adv_poor_signal = generate_advertisement_data(
        local_name="wohand_poor_signal", service_uuids=[], rssi=-100
    )
    inject_advertisement_with_time_and_source_connectable(
        switchbot_device_poor_signal,
        switchbot_adv_poor_signal,
        now,
        HCI0_SOURCE_ADDRESS,
        True,
    )

    assert (
        get_manager().async_ble_device_from_address(address, False)
        is switchbot_device_poor_signal
    )
    assert (
        get_manager().async_ble_device_from_address(address, True)
        is switchbot_device_poor_signal
    )
    switchbot_device_good_signal = generate_ble_device(address, "wohand_good_signal")
    switchbot_adv_good_signal = generate_advertisement_data(
        local_name="wohand_good_signal", service_uuids=[], rssi=-60
    )
    inject_advertisement_with_time_and_source_connectable(
        switchbot_device_good_signal,
        switchbot_adv_good_signal,
        now,
        "hci1",
        False,
    )

    assert (
        get_manager().async_ble_device_from_address(address, False)
        is switchbot_device_good_signal
    )
    assert (
        get_manager().async_ble_device_from_address(address, True)
        is switchbot_device_poor_signal
    )
    inject_advertisement_with_time_and_source_connectable(
        switchbot_device_good_signal,
        switchbot_adv_poor_signal,
        now,
        "hci0",
        False,
    )
    assert (
        get_manager().async_ble_device_from_address(address, False)
        is switchbot_device_good_signal
    )
    assert (
        get_manager().async_ble_device_from_address(address, True)
        is switchbot_device_poor_signal
    )
    switchbot_device_excellent_signal = generate_ble_device(
        address, "wohand_excellent_signal"
    )
    switchbot_adv_excellent_signal = generate_advertisement_data(
        local_name="wohand_excellent_signal", service_uuids=[], rssi=-25
    )

    inject_advertisement_with_time_and_source_connectable(
        switchbot_device_excellent_signal,
        switchbot_adv_excellent_signal,
        now,
        "hci2",
        False,
    )
    assert (
        get_manager().async_ble_device_from_address(address, False)
        is switchbot_device_excellent_signal
    )
    assert (
        get_manager().async_ble_device_from_address(address, True)
        is switchbot_device_poor_signal
    )


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_connectable_advertisement_can_be_retrieved_best_path_is_non_connectable(
    register_hci0_scanner: None,
    register_hci1_scanner: None,
) -> None:
    """
    Test we can still get a connectable BLEDevice when the best path is non-connectable.

    In this case the device is closer to a non-connectable scanner, but the
    at least one connectable scanner has the device in range.
    """
    address = "44:44:33:11:23:45"
    now = time.monotonic()
    switchbot_device_good_signal = generate_ble_device(address, "wohand_good_signal")
    switchbot_adv_good_signal = generate_advertisement_data(
        local_name="wohand_good_signal", service_uuids=[], rssi=-60
    )
    inject_advertisement_with_time_and_source_connectable(
        switchbot_device_good_signal,
        switchbot_adv_good_signal,
        now,
        HCI1_SOURCE_ADDRESS,
        False,
    )

    assert (
        get_manager().async_ble_device_from_address(address, False)
        is switchbot_device_good_signal
    )
    assert get_manager().async_ble_device_from_address(address, True) is None

    switchbot_device_poor_signal = generate_ble_device(address, "wohand_poor_signal")
    switchbot_adv_poor_signal = generate_advertisement_data(
        local_name="wohand_poor_signal", service_uuids=[], rssi=-100
    )
    inject_advertisement_with_time_and_source_connectable(
        switchbot_device_poor_signal,
        switchbot_adv_poor_signal,
        now,
        HCI0_SOURCE_ADDRESS,
        True,
    )

    assert (
        get_manager().async_ble_device_from_address(address, False)
        is switchbot_device_good_signal
    )
    assert (
        get_manager().async_ble_device_from_address(address, True)
        is switchbot_device_poor_signal
    )


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_switching_adapters_when_one_goes_away(
    register_hci0_scanner: None,
) -> None:
    """Test switching adapters when one goes away."""
    cancel_hci2 = get_manager().async_register_scanner(FakeScanner("hci2", "hci2"))

    address = "44:44:33:11:23:45"

    switchbot_device_good_signal = generate_ble_device(address, "wohand_good_signal")
    switchbot_adv_good_signal = generate_advertisement_data(
        local_name="wohand_good_signal", service_uuids=[], rssi=-60
    )
    inject_advertisement_with_source(
        switchbot_device_good_signal, switchbot_adv_good_signal, "hci2"
    )

    assert (
        get_manager().async_ble_device_from_address(address, True)
        is switchbot_device_good_signal
    )

    switchbot_device_poor_signal = generate_ble_device(address, "wohand_poor_signal")
    switchbot_adv_poor_signal = generate_advertisement_data(
        local_name="wohand_poor_signal", service_uuids=[], rssi=-100
    )
    inject_advertisement_with_source(
        switchbot_device_poor_signal,
        switchbot_adv_poor_signal,
        HCI0_SOURCE_ADDRESS,
    )

    # We want to prefer the good signal when we have options
    assert (
        get_manager().async_ble_device_from_address(address, True)
        is switchbot_device_good_signal
    )

    cancel_hci2()

    inject_advertisement_with_source(
        switchbot_device_poor_signal,
        switchbot_adv_poor_signal,
        HCI0_SOURCE_ADDRESS,
    )

    # Now that hci2 is gone, we should prefer the poor signal
    # since no poor signal is better than no signal
    assert (
        get_manager().async_ble_device_from_address(address, True)
        is switchbot_device_poor_signal
    )


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_switching_adapters_when_one_stop_scanning(
    register_hci0_scanner: None,
) -> None:
    """Test switching adapters when stops scanning."""
    hci2_scanner = FakeScanner("hci2", "hci2")
    cancel_hci2 = get_manager().async_register_scanner(hci2_scanner)

    address = "44:44:33:11:23:45"

    switchbot_device_good_signal = generate_ble_device(address, "wohand_good_signal")
    switchbot_adv_good_signal = generate_advertisement_data(
        local_name="wohand_good_signal", service_uuids=[], rssi=-60
    )
    inject_advertisement_with_source(
        switchbot_device_good_signal, switchbot_adv_good_signal, "hci2"
    )

    assert (
        get_manager().async_ble_device_from_address(address, True)
        is switchbot_device_good_signal
    )

    switchbot_device_poor_signal = generate_ble_device(address, "wohand_poor_signal")
    switchbot_adv_poor_signal = generate_advertisement_data(
        local_name="wohand_poor_signal", service_uuids=[], rssi=-100
    )
    inject_advertisement_with_source(
        switchbot_device_poor_signal,
        switchbot_adv_poor_signal,
        HCI0_SOURCE_ADDRESS,
    )

    # We want to prefer the good signal when we have options
    assert (
        get_manager().async_ble_device_from_address(address, True)
        is switchbot_device_good_signal
    )

    hci2_scanner.scanning = False

    inject_advertisement_with_source(
        switchbot_device_poor_signal,
        switchbot_adv_poor_signal,
        HCI0_SOURCE_ADDRESS,
    )

    # Now that hci2 has stopped scanning, we should prefer the poor signal
    # since poor signal is better than no signal
    assert (
        get_manager().async_ble_device_from_address(address, True)
        is switchbot_device_poor_signal
    )

    cancel_hci2()


@pytest.mark.usefixtures("enable_bluetooth", "macos_adapter")
@pytest.mark.asyncio
async def test_set_fallback_interval_small() -> None:
    """Test we can set the fallback advertisement interval."""
    assert (
        get_manager().async_get_fallback_availability_interval("44:44:33:11:23:12")
        is None
    )

    get_manager().async_set_fallback_availability_interval("44:44:33:11:23:12", 2.0)
    assert (
        get_manager().async_get_fallback_availability_interval("44:44:33:11:23:12")
        == 2.0
    )

    start_monotonic_time = time.monotonic()
    switchbot_device = generate_ble_device("44:44:33:11:23:12", "wohand")
    switchbot_adv = generate_advertisement_data(
        local_name="wohand", service_uuids=["cba20d00-224d-11e6-9fb8-0002a5d5c51b"]
    )
    switchbot_device_went_unavailable = False

    inject_advertisement_with_time_and_source(
        switchbot_device,
        switchbot_adv,
        start_monotonic_time,
        SOURCE_LOCAL,
    )

    def _switchbot_device_unavailable_callback(
        _address: BluetoothServiceInfoBleak,
    ) -> None:
        """Switchbot device unavailable callback."""
        nonlocal switchbot_device_went_unavailable
        switchbot_device_went_unavailable = True

    assert (
        get_manager().async_get_learned_advertising_interval("44:44:33:11:23:12")
        is None
    )

    switchbot_device_unavailable_cancel = get_manager().async_track_unavailable(
        _switchbot_device_unavailable_callback,
        switchbot_device.address,
        connectable=False,
    )

    monotonic_now = start_monotonic_time + 2
    with patch_bluetooth_time(
        monotonic_now + UNAVAILABLE_TRACK_SECONDS,
    ):
        async_fire_time_changed(utcnow() + timedelta(seconds=UNAVAILABLE_TRACK_SECONDS))
        await asyncio.sleep(0)

    assert switchbot_device_went_unavailable is True
    switchbot_device_unavailable_cancel()

    # We should forget fallback interval after it expires
    assert (
        get_manager().async_get_fallback_availability_interval("44:44:33:11:23:12")
        is None
    )


@pytest.mark.usefixtures("enable_bluetooth", "macos_adapter")
@pytest.mark.asyncio
async def test_set_fallback_interval_big() -> None:
    """Test we can set the fallback advertisement interval."""
    assert (
        get_manager().async_get_fallback_availability_interval("44:44:33:11:23:12")
        is None
    )

    # Force the interval to be really big and check it doesn't expire using the default
    # timeout (900)

    get_manager().async_set_fallback_availability_interval(
        "44:44:33:11:23:12", 604800.0
    )
    assert (
        get_manager().async_get_fallback_availability_interval("44:44:33:11:23:12")
        == 604800.0
    )

    start_monotonic_time = time.monotonic()
    switchbot_device = generate_ble_device("44:44:33:11:23:12", "wohand")
    switchbot_adv = generate_advertisement_data(
        local_name="wohand", service_uuids=["cba20d00-224d-11e6-9fb8-0002a5d5c51b"]
    )
    switchbot_device_went_unavailable = False

    inject_advertisement_with_time_and_source(
        switchbot_device,
        switchbot_adv,
        start_monotonic_time,
        SOURCE_LOCAL,
    )

    def _switchbot_device_unavailable_callback(
        _address: BluetoothServiceInfoBleak,
    ) -> None:
        """Switchbot device unavailable callback."""
        nonlocal switchbot_device_went_unavailable
        switchbot_device_went_unavailable = True

    assert (
        get_manager().async_get_learned_advertising_interval("44:44:33:11:23:12")
        is None
    )

    switchbot_device_unavailable_cancel = get_manager().async_track_unavailable(
        _switchbot_device_unavailable_callback,
        switchbot_device.address,
        connectable=False,
    )

    # Check that device hasn't expired after a day

    monotonic_now = start_monotonic_time + 86400
    with patch_bluetooth_time(
        monotonic_now + UNAVAILABLE_TRACK_SECONDS,
    ):
        async_fire_time_changed(utcnow() + timedelta(seconds=UNAVAILABLE_TRACK_SECONDS))
        await asyncio.sleep(0)

    assert switchbot_device_went_unavailable is False

    # Try again after it has expired

    monotonic_now = start_monotonic_time + 604800
    with patch_bluetooth_time(
        monotonic_now + UNAVAILABLE_TRACK_SECONDS,
    ):
        async_fire_time_changed(utcnow() + timedelta(seconds=UNAVAILABLE_TRACK_SECONDS))
        await asyncio.sleep(0)

    assert switchbot_device_went_unavailable is True

    switchbot_device_unavailable_cancel()  # type: ignore[unreachable]

    # We should forget fallback interval after it expires
    assert (
        get_manager().async_get_fallback_availability_interval("44:44:33:11:23:12")
        is None
    )


@pytest.mark.asyncio
async def test_subclassing_bluetooth_manager(caplog: pytest.LogCaptureFixture) -> None:
    """Test subclassing BluetoothManager."""
    slot_manager = BleakSlotManager()
    bluetooth_adapters = FakeBluetoothAdapters()

    class TestBluetoothManager(BluetoothManager):
        """
        Test class for BluetoothManager.

        This class implements _discover_service_info.
        """

        def _discover_service_info(
            self, service_info: BluetoothServiceInfoBleak
        ) -> None:
            """
            Discover a new service info.

            This method is intended to be overridden by subclasses.
            """

    TestBluetoothManager(bluetooth_adapters, slot_manager)
    assert "does not implement _discover_service_info" not in caplog.text

    class TestBluetoothManager2(BluetoothManager):
        """
        Test class for BluetoothManager.

        This class does not implement _discover_service_info.
        """

    TestBluetoothManager2(bluetooth_adapters, slot_manager)
    assert "does not implement _discover_service_info" in caplog.text
