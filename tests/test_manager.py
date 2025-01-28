"""Tests for the manager."""

import time
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from bleak_retry_connector import AllocationChange, Allocations, BleakSlotManager
from bluetooth_adapters.systems.linux import LinuxAdapters
from freezegun import freeze_time

from habluetooth import (
    BluetoothManager,
    HaBluetoothSlotAllocations,
    HaScannerRegistration,
    HaScannerRegistrationEvent,
    get_manager,
    set_manager,
)

from . import (
    async_fire_time_changed,
    generate_advertisement_data,
    generate_ble_device,
    inject_advertisement_with_source,
    utcnow,
)
from .conftest import FakeBluetoothAdapters, FakeScanner


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

    assert manager.async_current_allocations() == []
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
        HaBluetoothSlotAllocations("AA:BB:CC:DD:EE:00", 5, 4, ["44:44:33:11:23:12"])
    ]
    assert manager.async_current_allocations("AA:BB:CC:DD:EE:00") == [
        HaBluetoothSlotAllocations("AA:BB:CC:DD:EE:00", 5, 4, ["44:44:33:11:23:12"])
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
