"""Tests for the manager."""

import asyncio
import logging
import time
from collections.abc import Callable, Iterable
from datetime import timedelta
from typing import Any
from unittest.mock import ANY, AsyncMock, Mock, PropertyMock, patch

import pytest
from bleak.backends.scanner import AdvertisementData, BLEDevice
from bleak_retry_connector import AllocationChange, Allocations, BleakSlotManager
from bluetooth_adapters import ADAPTER_ADDRESS, ADAPTER_PASSIVE_SCAN
from bluetooth_adapters.systems.linux import LinuxAdapters
from freezegun import freeze_time

from habluetooth import (
    FALLBACK_MAXIMUM_STALE_ADVERTISEMENT_SECONDS,
    TRACKER_BUFFERING_WOBBLE_SECONDS,
    UNAVAILABLE_TRACK_SECONDS,
    BluetoothManager,
    BluetoothReachabilityIntent,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
    HaBluetoothSlotAllocations,
    HaScannerModeChange,
    HaScannerRegistration,
    HaScannerRegistrationEvent,
    get_manager,
    set_manager,
)
from habluetooth.central_manager import CentralBluetoothManager

from . import (
    HCI0_SOURCE_ADDRESS,
    HCI1_SOURCE_ADDRESS,
    NON_CONNECTABLE_REMOTE_SOURCE_ADDRESS,
    InjectableRemoteScanner,
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
        (("hci1", "00:00:00:00:00:00", False),),
        (("hci2", "00:00:00:00:00:00", False),),
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
        msg = "This is a test"
        raise ValueError(msg)

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
async def test_check_unavailable_materializes_each_scanner_once() -> None:
    """
    _async_check_unavailable must hit each scanner's discovered_addresses once.

    Regression for https://github.com/Bluetooth-Devices/habluetooth/issues/505:
    the prior two-pass loop accessed every connectable scanner twice per
    cycle, and ``HaScanner.discovered_addresses`` rebuilds bleak's
    discovered-devices dict on every access.
    """
    manager = get_manager()
    address = "44:44:33:11:23:12"

    connectable_calls = 0
    non_connectable_calls = 0

    class CountingConnectable(FakeScanner):
        @property
        def discovered_addresses(self) -> Iterable[str]:
            nonlocal connectable_calls
            connectable_calls += 1
            return (address,)

    class CountingNonConnectable(FakeScanner):
        @property
        def discovered_addresses(self) -> Iterable[str]:
            nonlocal non_connectable_calls
            non_connectable_calls += 1
            return (address,)

    connectable = CountingConnectable("hci0", "hci0", connectable=True)
    non_connectable = CountingNonConnectable("hci1", "hci1", connectable=False)
    cancel_c = manager.async_register_scanner(connectable)
    cancel_n = manager.async_register_scanner(non_connectable)

    manager._async_check_unavailable()

    assert connectable_calls == 1
    assert non_connectable_calls == 1

    cancel_c()
    cancel_n()


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
        msg = "This is a test"
        raise ValueError(msg)

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
        msg = "This is a test"
        raise ValueError(msg)

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
@pytest.mark.usefixtures("enable_bluetooth")
async def test_async_register_scanner_mode_change_callback(
    register_hci0_scanner: None,
    register_hci1_scanner: None,
) -> None:
    """Test bluetooth async_register_scanner_mode_change_callback handles failures."""
    manager = get_manager()
    assert manager._loop is not None

    scanners = manager.async_current_scanners()
    assert len(scanners) == 2
    scanner = scanners[0]

    failed_mode_callbacks: list[HaScannerModeChange] = []

    def _failing_callback(mode_change: HaScannerModeChange) -> None:
        """Failing callback."""
        failed_mode_callbacks.append(mode_change)
        msg = "This is a test"
        raise ValueError(msg)

    ok_mode_callbacks: list[HaScannerModeChange] = []

    def _ok_callback(mode_change: HaScannerModeChange) -> None:
        """Ok callback."""
        ok_mode_callbacks.append(mode_change)

    cancel1 = manager.async_register_scanner_mode_change_callback(
        _failing_callback, None
    )
    # Make sure the second callback still works if the first one fails and
    # raises an exception
    cancel2 = manager.async_register_scanner_mode_change_callback(_ok_callback, None)

    # Test specific source callback
    source_specific_callbacks: list[HaScannerModeChange] = []

    def _source_specific_callback(mode_change: HaScannerModeChange) -> None:
        """Source specific callback."""
        source_specific_callbacks.append(mode_change)

    cancel3 = manager.async_register_scanner_mode_change_callback(
        _source_specific_callback, scanner.source
    )

    # Change requested mode

    scanner.set_requested_mode(BluetoothScanningMode.ACTIVE)

    assert len(ok_mode_callbacks) == 1
    assert ok_mode_callbacks[0].scanner == scanner
    assert ok_mode_callbacks[0].requested_mode == BluetoothScanningMode.ACTIVE
    assert ok_mode_callbacks[0].current_mode == scanner.current_mode

    assert len(failed_mode_callbacks) == 1
    assert len(source_specific_callbacks) == 1

    # Change current mode
    scanner.set_current_mode(BluetoothScanningMode.ACTIVE)

    assert len(ok_mode_callbacks) == 2
    assert ok_mode_callbacks[1].scanner == scanner
    assert ok_mode_callbacks[1].current_mode == BluetoothScanningMode.ACTIVE

    assert len(failed_mode_callbacks) == 2
    assert len(source_specific_callbacks) == 2

    # No change when setting the same mode
    scanner.set_current_mode(BluetoothScanningMode.ACTIVE)
    assert len(ok_mode_callbacks) == 2

    cancel1()
    cancel2()
    cancel3()


_PASSIVE_WARNING = "is in passive-only mode"


@pytest.mark.asyncio
async def test_active_scan_warns_about_existing_passive_scanner(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Registering an active scan warns when a passive-only scanner exists."""
    manager = get_manager()
    passive = FakeScanner(
        "AA:BB:CC:DD:EE:01", "hci1", requested_mode=BluetoothScanningMode.PASSIVE
    )
    cancel_scanner = manager.async_register_scanner(passive)
    try:
        with caplog.at_level(logging.WARNING):
            cancel_scan = manager.async_register_active_scan("AA:BB:CC:DD:EE:99")
        assert _PASSIVE_WARNING in caplog.text
        assert passive.name in caplog.text
    finally:
        cancel_scan()
        cancel_scanner()


@pytest.mark.asyncio
async def test_scanner_going_passive_after_active_scan_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A scanner becoming passive while active scans exist warns once."""
    manager = get_manager()
    cancel_scan = manager.async_register_active_scan("AA:BB:CC:DD:EE:99")
    auto = FakeScanner(
        "AA:BB:CC:DD:EE:02", "hci2", requested_mode=BluetoothScanningMode.AUTO
    )
    cancel_scanner = manager.async_register_scanner(auto)
    try:
        with caplog.at_level(logging.WARNING):
            # Current-mode toggles on an AUTO scanner must not warn.
            auto.set_current_mode(BluetoothScanningMode.ACTIVE)
            assert _PASSIVE_WARNING not in caplog.text
            # Flipping the requested mode to passive warns.
            auto.set_requested_mode(BluetoothScanningMode.PASSIVE)
        assert caplog.text.count(_PASSIVE_WARNING) == 1
        assert auto.name in caplog.text
    finally:
        cancel_scanner()
        cancel_scan()


@pytest.mark.asyncio
async def test_passive_scanner_warning_dedupes_per_source(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One passive scanner warns once across many active-scan registrations."""
    manager = get_manager()
    passive = FakeScanner(
        "AA:BB:CC:DD:EE:03", "hci3", requested_mode=BluetoothScanningMode.PASSIVE
    )
    cancel_scanner = manager.async_register_scanner(passive)
    cancels: list[Callable[[], None]] = []
    try:
        with caplog.at_level(logging.WARNING):
            cancels.extend(
                manager.async_register_active_scan(addr)
                for addr in ("AA:BB:CC:DD:EE:91", "AA:BB:CC:DD:EE:92")
            )
        assert caplog.text.count(_PASSIVE_WARNING) == 1
    finally:
        for cancel in cancels:
            cancel()
        cancel_scanner()


@pytest.mark.asyncio
async def test_active_scan_no_warning_for_auto_or_active_scanner(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No warning when the only scanners are auto or active."""
    manager = get_manager()
    auto = FakeScanner(
        "AA:BB:CC:DD:EE:04", "hci4", requested_mode=BluetoothScanningMode.AUTO
    )
    active = FakeScanner(
        "AA:BB:CC:DD:EE:05", "hci5", requested_mode=BluetoothScanningMode.ACTIVE
    )
    cancel_auto = manager.async_register_scanner(auto)
    cancel_active = manager.async_register_scanner(active)
    try:
        with caplog.at_level(logging.WARNING):
            cancel_scan = manager.async_register_active_scan("AA:BB:CC:DD:EE:99")
        assert _PASSIVE_WARNING not in caplog.text
    finally:
        cancel_scan()
        cancel_auto()
        cancel_active()


@pytest.mark.asyncio
async def test_passive_scanner_warning_resets_after_unregister(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Re-registering a passive scanner warns again after it is unregistered."""
    manager = get_manager()
    cancel_scan = manager.async_register_active_scan("AA:BB:CC:DD:EE:99")
    try:
        with caplog.at_level(logging.WARNING):
            cancel_scanner = manager.async_register_scanner(
                FakeScanner(
                    "AA:BB:CC:DD:EE:06",
                    "hci6",
                    requested_mode=BluetoothScanningMode.PASSIVE,
                )
            )
        assert caplog.text.count(_PASSIVE_WARNING) == 1
        cancel_scanner()
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            cancel_scanner2 = manager.async_register_scanner(
                FakeScanner(
                    "AA:BB:CC:DD:EE:06",
                    "hci6",
                    requested_mode=BluetoothScanningMode.PASSIVE,
                )
            )
        assert caplog.text.count(_PASSIVE_WARNING) == 1
        cancel_scanner2()
    finally:
        cancel_scan()


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
async def test_async_unregister_scanner_is_idempotent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Double-invoking the cancel callback must not raise."""
    manager = get_manager()
    hci3_scanner = FakeScanner("AA:BB:CC:DD:EE:33", "hci3")
    hci3_scanner.connectable = True
    cancel = manager.async_register_scanner(hci3_scanner, connection_slots=5)

    cancel()
    assert hci3_scanner not in manager.async_current_scanners()

    with caplog.at_level("DEBUG", logger="habluetooth.manager"):
        cancel()

    assert any("already unregistered" in record.message for record in caplog.records)


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
        "auto_scheduler": ANY,
        "connectable_history": ANY,
        "scanners": [
            {
                "connect_failures": {},
                "connect_in_progress": {},
                "connect_completed_total": 0,
                "connect_failed_total": 0,
                "last_connect_completed_time": 0.0,
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
async def test_stale_does_not_switch_to_much_weaker_scanner(
    register_hci0_scanner: None,
    register_hci1_scanner: None,
) -> None:
    """
    A far weaker scanner must not steal a strong owner on a single stale interval.

    Regression for #568: scan-response-only sensors behind many active proxies
    oscillated because a weaker proxy holding a stale capture won the receive-time
    staleness check on a single missed interval.
    """
    address = "44:44:33:11:23:42"
    start = 50.0
    strong = generate_ble_device(address, "strong_hci0")
    strong_adv = generate_advertisement_data(
        local_name="strong_hci0", service_uuids=[], rssi=-46
    )
    inject_advertisement_with_time_and_source(
        strong, strong_adv, start, HCI0_SOURCE_ADDRESS
    )
    get_manager().async_set_fallback_availability_interval(address, 10)  # stale=15

    weak = generate_ble_device(address, "weak_hci1")
    weak_adv = generate_advertisement_data(
        local_name="weak_hci1", service_uuids=[], rssi=-90
    )
    # Just past the normal stale window: a 44 dB weaker scanner must NOT win.
    inject_advertisement_with_time_and_source(
        weak,
        weak_adv,
        start + 10 + TRACKER_BUFFERING_WOBBLE_SECONDS + 1,
        HCI1_SOURCE_ADDRESS,
    )
    assert get_manager().async_ble_device_from_address(address, True) is strong


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_stale_switches_to_weaker_scanner_once_durably_gone(
    register_hci0_scanner: None,
    register_hci1_scanner: None,
) -> None:
    """
    A device that moved into weak-only coverage still hands off after a longer wait.

    The weaker scanner is held off on a single stale interval but takes over once
    the owner has been silent for stale_seconds * DURABLY_GONE_STALE_FACTOR.
    """
    address = "44:44:33:11:23:43"
    start = 50.0
    strong = generate_ble_device(address, "strong_hci0")
    strong_adv = generate_advertisement_data(
        local_name="strong_hci0", service_uuids=[], rssi=-46
    )
    inject_advertisement_with_time_and_source(
        strong, strong_adv, start, HCI0_SOURCE_ADDRESS
    )
    # stale_seconds = 15, durably-gone = 15 * 2.5 = 37.5
    get_manager().async_set_fallback_availability_interval(address, 10)

    weak = generate_ble_device(address, "weak_hci1")
    weak_adv = generate_advertisement_data(
        local_name="weak_hci1", service_uuids=[], rssi=-90
    )
    # Within the durably-gone window: still the strong owner.
    inject_advertisement_with_time_and_source(
        weak, weak_adv, start + 30, HCI1_SOURCE_ADDRESS
    )
    assert get_manager().async_ble_device_from_address(address, True) is strong
    # Past the durably-gone window: the weak scanner finally takes over.
    inject_advertisement_with_time_and_source(
        weak, weak_adv, start + 40, HCI1_SOURCE_ADDRESS
    )
    assert get_manager().async_ble_device_from_address(address, True) is weak


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_stale_switches_to_comparable_scanner_at_normal_window(
    register_hci0_scanner: None,
    register_hci1_scanner: None,
) -> None:
    """
    A comparable scanner takes over a weak owner at the normal stale window.

    The owner is weak (below STRONG_OWNER_STALE_RSSI), so its silence is
    treated as the device possibly being gone and a comparable scanner is
    allowed to take over at the normal window. A strong owner is protected
    (see test_stale_keeps_strong_owner_against_comparable_scanner).
    """
    address = "44:44:33:11:23:44"
    start = 50.0
    owner = generate_ble_device(address, "owner_hci0")
    owner_adv = generate_advertisement_data(
        local_name="owner_hci0", service_uuids=[], rssi=-90
    )
    inject_advertisement_with_time_and_source(
        owner, owner_adv, start, HCI0_SOURCE_ADDRESS
    )
    get_manager().async_set_fallback_availability_interval(address, 10)

    # Weak owner, comparable challenger: ordinary handoff at the normal window.
    comparable = generate_ble_device(address, "comparable_hci1")
    comparable_adv = generate_advertisement_data(
        local_name="comparable_hci1", service_uuids=[], rssi=-95
    )
    inject_advertisement_with_time_and_source(
        comparable,
        comparable_adv,
        start + 10 + TRACKER_BUFFERING_WOBBLE_SECONDS + 1,
        HCI1_SOURCE_ADDRESS,
    )
    assert get_manager().async_ble_device_from_address(address, True) is comparable


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_stale_keeps_strong_owner_against_comparable_scanner(
    register_hci0_scanner: None,
    register_hci1_scanner: None,
) -> None:
    """
    A strong/close owner is not stolen by a comparable scanner on a stale interval.

    Regression for the stationary-device flap: a strong owner (>=
    STRONG_OWNER_STALE_RSSI) that briefly goes quiet is almost certainly
    still there, so a merely-comparable scanner must wait for durable
    absence rather than steal on one missed interval.
    """
    address = "44:44:33:11:23:47"
    start = 50.0
    strong = generate_ble_device(address, "strong_hci0")
    strong_adv = generate_advertisement_data(
        local_name="strong_hci0", service_uuids=[], rssi=-50
    )
    inject_advertisement_with_time_and_source(
        strong, strong_adv, start, HCI0_SOURCE_ADDRESS
    )
    get_manager().async_set_fallback_availability_interval(address, 10)  # stale=15

    comparable = generate_ble_device(address, "comparable_hci1")
    comparable_adv = generate_advertisement_data(
        local_name="comparable_hci1", service_uuids=[], rssi=-60
    )
    # Just past the normal stale window: a comparable scanner must NOT steal
    # the strong owner.
    inject_advertisement_with_time_and_source(
        comparable,
        comparable_adv,
        start + 10 + TRACKER_BUFFERING_WOBBLE_SECONDS + 1,
        HCI1_SOURCE_ADDRESS,
    )
    assert get_manager().async_ble_device_from_address(address, True) is strong


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_stale_strong_owner_yields_to_comparable_when_durably_gone(
    register_hci0_scanner: None,
    register_hci1_scanner: None,
) -> None:
    """A strong owner that is durably silent still hands off to a comparable scanner."""
    address = "44:44:33:11:23:48"
    start = 50.0
    strong = generate_ble_device(address, "strong_hci0")
    strong_adv = generate_advertisement_data(
        local_name="strong_hci0", service_uuids=[], rssi=-50
    )
    inject_advertisement_with_time_and_source(
        strong, strong_adv, start, HCI0_SOURCE_ADDRESS
    )
    # stale_seconds = 15, durably-gone = 15 * 2.5 = 37.5
    get_manager().async_set_fallback_availability_interval(address, 10)

    comparable = generate_ble_device(address, "comparable_hci1")
    comparable_adv = generate_advertisement_data(
        local_name="comparable_hci1", service_uuids=[], rssi=-60
    )
    # Within the durably-gone window: strong owner kept.
    inject_advertisement_with_time_and_source(
        comparable, comparable_adv, start + 30, HCI1_SOURCE_ADDRESS
    )
    assert get_manager().async_ble_device_from_address(address, True) is strong
    # Past durably-gone: the comparable scanner finally takes over.
    inject_advertisement_with_time_and_source(
        comparable, comparable_adv, start + 40, HCI1_SOURCE_ADDRESS
    )
    assert get_manager().async_ble_device_from_address(address, True) is comparable


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_stale_strong_owner_yields_to_stronger_scanner(
    register_hci0_scanner: None,
    register_hci1_scanner: None,
) -> None:
    """A strong owner still yields immediately to a materially stronger scanner."""
    address = "44:44:33:11:23:49"
    start = 50.0
    strong = generate_ble_device(address, "strong_hci0")
    strong_adv = generate_advertisement_data(
        local_name="strong_hci0", service_uuids=[], rssi=-50
    )
    inject_advertisement_with_time_and_source(
        strong, strong_adv, start, HCI0_SOURCE_ADDRESS
    )
    get_manager().async_set_fallback_availability_interval(address, 10)

    # >16 dB stronger: the RSSI path takes over immediately, strong owner or not.
    stronger = generate_ble_device(address, "stronger_hci1")
    stronger_adv = generate_advertisement_data(
        local_name="stronger_hci1", service_uuids=[], rssi=-20
    )
    inject_advertisement_with_time_and_source(
        stronger,
        stronger_adv,
        start + 10 + TRACKER_BUFFERING_WOBBLE_SECONDS + 1,
        HCI1_SOURCE_ADDRESS,
    )
    assert get_manager().async_ble_device_from_address(address, True) is stronger


def _rssi_adv(rssi: int) -> AdvertisementData:
    return generate_advertisement_data(
        local_name="smoothing", service_uuids=[], rssi=rssi
    )


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_rssi_smoothing_ignores_single_spike(
    register_hci0_scanner: None,
    register_hci1_scanner: None,
) -> None:
    """
    A one-off RSSI spike from a challenger does not flip a stationary owner.

    Instantaneously the spike clears the 16 dB threshold, but the smoothed
    per-source RSSI does not, so ownership stays put.
    """
    manager = get_manager()
    address = "44:44:33:11:23:50"
    t = 50.0
    owner = generate_ble_device(address, "owner_hci0")
    challenger = generate_ble_device(address, "challenger_hci1")

    inject_advertisement_with_time_and_source(
        owner, _rssi_adv(-50), t, HCI0_SOURCE_ADDRESS
    )
    # Challenger first seen weaker (-60), so it enters the smoothing bucket.
    inject_advertisement_with_time_and_source(
        challenger, _rssi_adv(-60), t + 1, HCI1_SOURCE_ADDRESS
    )
    assert manager.async_ble_device_from_address(address, True) is owner

    # Single spike to -30 (instantaneously 20 dB stronger than the owner):
    # smoothed hci1 = 0.3*-30 + 0.7*-60 = -51, still below -50 + threshold,
    # so it must NOT switch.
    inject_advertisement_with_time_and_source(
        challenger, _rssi_adv(-30), t + 2, HCI1_SOURCE_ADDRESS
    )
    assert manager.async_ble_device_from_address(address, True) is owner


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_rssi_smoothing_switches_on_sustained_stronger(
    register_hci0_scanner: None,
    register_hci1_scanner: None,
) -> None:
    """A sustained stronger challenger wins once its smoothed RSSI crosses."""
    manager = get_manager()
    address = "44:44:33:11:23:51"
    t = 50.0
    owner = generate_ble_device(address, "owner_hci0")
    challenger = generate_ble_device(address, "challenger_hci1")

    inject_advertisement_with_time_and_source(
        owner, _rssi_adv(-50), t, HCI0_SOURCE_ADDRESS
    )
    inject_advertisement_with_time_and_source(
        challenger, _rssi_adv(-55), t + 1, HCI1_SOURCE_ADDRESS
    )
    assert manager.async_ble_device_from_address(address, True) is owner

    # Sustained -20 (30 dB stronger): the smoothed value climbs past the
    # threshold after a few samples and ownership flips.
    for i in range(8):
        inject_advertisement_with_time_and_source(
            challenger, _rssi_adv(-20), t + 2 + i, HCI1_SOURCE_ADDRESS
        )
    assert manager.async_ble_device_from_address(address, True) is challenger


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_rssi_smoothing_first_sighting_switches_immediately(
    register_hci0_scanner: None,
    register_hci1_scanner: None,
) -> None:
    """A first-seen challenger with a real margin switches at once (instantaneous)."""
    manager = get_manager()
    address = "44:44:33:11:23:52"
    t = 50.0
    owner = generate_ble_device(address, "owner_hci0")
    challenger = generate_ble_device(address, "challenger_hci1")

    inject_advertisement_with_time_and_source(
        owner, _rssi_adv(-50), t, HCI0_SOURCE_ADDRESS
    )
    # No prior smoothed history for the challenger: it seeds at its
    # instantaneous -20, which is a genuine 30 dB margin, so it wins now.
    inject_advertisement_with_time_and_source(
        challenger, _rssi_adv(-20), t + 1, HCI1_SOURCE_ADDRESS
    )
    assert manager.async_ble_device_from_address(address, True) is challenger


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_rssi_smoothing_bucket_evicted_with_history(
    register_hci0_scanner: None,
    register_hci1_scanner: None,
) -> None:
    """The smoothed-RSSI bucket is allocated cross-source and cleared with history."""
    manager = get_manager()
    address = "44:44:33:11:23:53"
    t = 50.0
    owner = generate_ble_device(address, "owner_hci0")
    challenger = generate_ble_device(address, "challenger_hci1")

    inject_advertisement_with_time_and_source(
        owner, _rssi_adv(-50), t, HCI0_SOURCE_ADDRESS
    )
    # Single source so far: no bucket allocated.
    assert address not in manager._smoothed_rssi
    inject_advertisement_with_time_and_source(
        challenger, _rssi_adv(-60), t + 1, HCI1_SOURCE_ADDRESS
    )
    # Cross-source seen: bucket now holds both sources.
    assert set(manager._smoothed_rssi[address]) == {
        HCI0_SOURCE_ADDRESS,
        HCI1_SOURCE_ADDRESS,
    }

    manager.async_clear_advertisement_history(address)
    assert address not in manager._smoothed_rssi


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_rssi_smoothing_source_evicted_on_unregister(
    register_hci0_scanner: None,
    register_hci1_scanner: None,
) -> None:
    """Unregistering a scanner drops its source from every smoothed bucket."""
    manager = get_manager()
    address = "44:44:33:11:23:54"
    t = 50.0
    owner = generate_ble_device(address, "owner_hci0")
    challenger = generate_ble_device(address, "challenger_hci1")

    inject_advertisement_with_time_and_source(
        owner, _rssi_adv(-50), t, HCI0_SOURCE_ADDRESS
    )
    inject_advertisement_with_time_and_source(
        challenger, _rssi_adv(-60), t + 1, HCI1_SOURCE_ADDRESS
    )
    assert HCI1_SOURCE_ADDRESS in manager._smoothed_rssi[address]

    def _unregister(source: str) -> None:
        for scanner in list(manager._sources.values()):
            if scanner.source == source:
                manager._async_unregister_scanner_internal(
                    manager._connectable_scanners, scanner, None
                )

    _unregister(HCI1_SOURCE_ADDRESS)
    # One source remains, so the bucket survives without the dropped source.
    assert HCI1_SOURCE_ADDRESS not in manager._smoothed_rssi[address]
    assert HCI0_SOURCE_ADDRESS in manager._smoothed_rssi[address]

    # Dropping the last source empties the bucket; the address is removed so
    # the map can return to truly empty and re-enable the proxy-free fast path.
    _unregister(HCI0_SOURCE_ADDRESS)
    assert address not in manager._smoothed_rssi
    assert not manager._smoothed_rssi


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_rssi_smoothing_skips_unregistered_old_source(
    register_hci0_scanner: None,
    register_hci1_scanner: None,
) -> None:
    """
    A new bucket is not seeded against a source that has unregistered.

    An old source lingers in _all_history after it unregisters (the
    unavailable sweep clears it later); seeding its stale RSSI would
    reintroduce the source the unregister cleanup just dropped.
    """
    manager = get_manager()
    address = "44:44:33:11:23:55"
    t = 50.0
    owner = generate_ble_device(address, "owner_hci0")
    challenger = generate_ble_device(address, "challenger_hci1")

    # Seen from hci0 only, so no bucket exists yet.
    inject_advertisement_with_time_and_source(
        owner, _rssi_adv(-50), t, HCI0_SOURCE_ADDRESS
    )
    assert address not in manager._smoothed_rssi

    # hci0 unregisters, but its advertisement lingers in _all_history.
    for scanner in list(manager._sources.values()):
        if scanner.source == HCI0_SOURCE_ADDRESS:
            manager._async_unregister_scanner_internal(
                manager._connectable_scanners, scanner, None
            )
    assert manager._all_history[address].source == HCI0_SOURCE_ADDRESS

    # hci1 now advertises; the dead hci0 source must not be seeded, so no
    # bucket is created for what is now effectively a single-source device.
    inject_advertisement_with_time_and_source(
        challenger, _rssi_adv(-60), t + 1, HCI1_SOURCE_ADDRESS
    )
    assert address not in manager._smoothed_rssi


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

    # Force the interval to be really big and check it doesn't expire using
    # the default timeout of 900 seconds.

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


@pytest.mark.asyncio
async def test_is_operating_degraded_on_linux_with_mgmt() -> None:
    """Test is_operating_degraded returns False on Linux with mgmt control."""
    mock_bluetooth_adapters = FakeBluetoothAdapters()
    manager = BluetoothManager(
        mock_bluetooth_adapters,
        slot_manager=Mock(),
    )

    with (
        patch("habluetooth.manager.IS_LINUX", True),
        patch.object(manager, "_mgmt_ctl", Mock()),
    ):
        # Mock mgmt_ctl being available
        assert manager.is_operating_degraded() is False


@pytest.mark.asyncio
async def test_is_operating_degraded_on_linux_without_mgmt() -> None:
    """Test is_operating_degraded returns True on Linux without mgmt control."""
    mock_bluetooth_adapters = FakeBluetoothAdapters()
    manager = BluetoothManager(
        mock_bluetooth_adapters,
        slot_manager=Mock(),
    )

    with patch("habluetooth.manager.IS_LINUX", True):
        # mgmt_ctl is None by default
        assert manager._mgmt_ctl is None
        assert manager.is_operating_degraded() is True


@pytest.mark.asyncio
async def test_is_operating_degraded_on_non_linux() -> None:
    """Test is_operating_degraded returns False on non-Linux systems."""
    mock_bluetooth_adapters = FakeBluetoothAdapters()
    manager = BluetoothManager(
        mock_bluetooth_adapters,
        slot_manager=Mock(),
    )

    with patch("habluetooth.manager.IS_LINUX", False):
        # Should return False regardless of mgmt_ctl state
        assert manager.is_operating_degraded() is False

        # Even with mgmt_ctl set
        manager._mgmt_ctl = Mock()
        assert manager.is_operating_degraded() is False


@pytest.mark.asyncio
async def test_is_operating_degraded_after_permission_error() -> None:
    """Test is_operating_degraded after mgmt setup fails with permission error."""
    mock_bluetooth_adapters = FakeBluetoothAdapters()
    manager = BluetoothManager(
        mock_bluetooth_adapters,
        slot_manager=Mock(),
    )

    with (
        patch("habluetooth.manager.IS_LINUX", True),
        patch("habluetooth.manager.MGMTBluetoothCtl") as mock_mgmt_class,
    ):
        # Make setup fail with permission error
        mock_mgmt_instance = Mock()
        mock_mgmt_instance.setup = AsyncMock(
            side_effect=PermissionError("No permission")
        )
        mock_mgmt_class.return_value = mock_mgmt_instance

        # Setup should handle the error and set mgmt_ctl to None
        await manager.async_setup()

        # Should be in degraded mode
        assert manager._mgmt_ctl is None
        assert manager.is_operating_degraded() is True


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_bluetooth")
async def test_async_scanner_count_includes_non_connectable(
    register_hci0_scanner: None,
    register_non_connectable_scanner: None,
) -> None:
    """Connectable count excludes non-connectable; full count includes both."""
    manager = get_manager()
    assert manager.async_scanner_count(connectable=True) == 1
    assert manager.async_scanner_count(connectable=False) == 2


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_bluetooth")
async def test_async_address_present_non_connectable_history(
    register_non_connectable_scanner: None,
) -> None:
    """async_address_present(connectable=False) reads the all-history map."""
    manager = get_manager()
    address = "44:44:33:11:23:99"
    device = generate_ble_device(address, "wohand")
    adv = generate_advertisement_data(local_name="wohand", service_uuids=[])
    inject_advertisement_with_time_and_source_connectable(
        device, adv, time.monotonic(), "AA:BB:CC:DD:EE:FF", False
    )
    assert manager.async_address_present(address, connectable=False) is True
    assert manager.async_address_present(address, connectable=True) is False
    missing = "00:00:00:00:00:00"
    assert manager.async_address_present(missing, connectable=False) is False


@pytest.mark.asyncio
async def test_async_track_unavailable_connectable_branch() -> None:
    """connectable=True routes the callback to the connectable callback map."""
    manager = get_manager()

    def _cb(_info: BluetoothServiceInfoBleak) -> None:
        return

    address = "11:22:33:44:55:66"
    cancel = manager.async_track_unavailable(_cb, address, connectable=True)
    try:
        assert _cb in manager._connectable_unavailable_callbacks[address]
        assert address not in manager._unavailable_callbacks
    finally:
        cancel()
    assert address not in manager._connectable_unavailable_callbacks


@pytest.mark.asyncio
async def test_async_current_allocations_unknown_source_returns_empty() -> None:
    """Querying an unknown source returns [] rather than None."""
    manager = get_manager()
    assert manager.async_current_allocations("not-a-real-source") == []


@pytest.mark.asyncio
async def test_async_recover_failed_adapters_skips_when_lock_held() -> None:
    """If recovery is already in flight, a concurrent call is a no-op."""
    manager = get_manager()
    with patch.object(
        manager, "async_get_bluetooth_adapters", new=AsyncMock()
    ) as mock_get:
        await manager._recovery_lock.acquire()
        try:
            await manager._async_recover_failed_adapters()
        finally:
            manager._recovery_lock.release()
    mock_get.assert_not_called()


@pytest.mark.asyncio
async def test_async_get_bluetooth_adapters_cached_false_triggers_refresh() -> None:
    """cached=False forces a refresh of the underlying adapter source."""
    manager = get_manager()
    assert manager._bluetooth_adapters is not None
    with patch.object(
        manager._bluetooth_adapters, "refresh", new=AsyncMock()
    ) as mock_refresh:
        await manager.async_get_bluetooth_adapters(cached=False)
    mock_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_async_refresh_adapters_propagates_exception_to_waiters() -> None:
    """Concurrent callers must see the refresh exception, not silent success."""
    manager = get_manager()
    assert manager._bluetooth_adapters is not None
    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()

    async def slow_failing_refresh() -> None:
        refresh_started.set()
        await release_refresh.wait()
        msg = "boom"
        raise RuntimeError(msg)

    with patch.object(manager._bluetooth_adapters, "refresh", new=slow_failing_refresh):
        leader = asyncio.create_task(manager._async_refresh_adapters())
        await refresh_started.wait()
        waiter_a = asyncio.create_task(manager._async_refresh_adapters())
        waiter_b = asyncio.create_task(manager._async_refresh_adapters())
        # Yield so waiters register on the shared future.
        await asyncio.sleep(0)
        release_refresh.set()
        with pytest.raises(RuntimeError, match="boom"):
            await leader
        with pytest.raises(RuntimeError, match="boom"):
            await waiter_a
        with pytest.raises(RuntimeError, match="boom"):
            await waiter_b
    # Shared future must be cleared so the next call refreshes again.
    assert manager._adapter_refresh_future is None


@pytest.mark.asyncio
async def test_async_refresh_adapters_success_resolves_waiters() -> None:
    """Concurrent callers all see success and share the same refresh call."""
    manager = get_manager()
    assert manager._bluetooth_adapters is not None
    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()
    call_count = 0

    async def slow_refresh() -> None:
        nonlocal call_count
        call_count += 1
        refresh_started.set()
        await release_refresh.wait()

    with patch.object(manager._bluetooth_adapters, "refresh", new=slow_refresh):
        leader = asyncio.create_task(manager._async_refresh_adapters())
        await refresh_started.wait()
        waiter_a = asyncio.create_task(manager._async_refresh_adapters())
        waiter_b = asyncio.create_task(manager._async_refresh_adapters())
        await asyncio.sleep(0)
        release_refresh.set()
        await leader
        await waiter_a
        await waiter_b
    assert call_count == 1
    assert manager._adapter_refresh_future is None


@pytest.mark.asyncio
async def test_async_refresh_adapters_leader_cancellation_does_not_silently_succeed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Leader cancellation must not let waiters proceed as if refresh succeeded."""
    manager = get_manager()
    assert manager._bluetooth_adapters is not None
    refresh_started = asyncio.Event()

    async def hanging_refresh() -> None:
        refresh_started.set()
        await asyncio.Event().wait()  # never resolves

    with patch.object(manager._bluetooth_adapters, "refresh", new=hanging_refresh):
        leader = asyncio.create_task(manager._async_refresh_adapters())
        await refresh_started.wait()
        waiter = asyncio.create_task(manager._async_refresh_adapters())
        await asyncio.sleep(0)
        leader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await leader
        # Waiter must observe a CancelledError, not silently complete.
        with pytest.raises(asyncio.CancelledError):
            await waiter
    assert manager._adapter_refresh_future is None


@pytest.mark.asyncio
async def test_async_refresh_adapters_waiter_cancellation_does_not_break_leader() -> (
    None
):
    """Cancelling one waiter must not strand the leader or other siblings."""
    manager = get_manager()
    assert manager._bluetooth_adapters is not None
    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()

    async def slow_refresh() -> None:
        refresh_started.set()
        await release_refresh.wait()

    with patch.object(manager._bluetooth_adapters, "refresh", new=slow_refresh):
        leader = asyncio.create_task(manager._async_refresh_adapters())
        await refresh_started.wait()
        waiter_a = asyncio.create_task(manager._async_refresh_adapters())
        waiter_b = asyncio.create_task(manager._async_refresh_adapters())
        await asyncio.sleep(0)
        waiter_a.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter_a
        # Leader and surviving waiter must still complete normally.
        release_refresh.set()
        await leader
        await waiter_b
    assert manager._adapter_refresh_future is None


@pytest.mark.asyncio
async def test_async_refresh_adapters_adapters_property_failure_propagates() -> None:
    """Property access failure after refresh() must not strand waiters."""
    manager = get_manager()
    assert manager._bluetooth_adapters is not None
    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()

    async def slow_refresh() -> None:
        refresh_started.set()
        await release_refresh.wait()

    failing_adapters = PropertyMock(side_effect=RuntimeError("adapters boom"))
    with (
        patch.object(manager._bluetooth_adapters, "refresh", new=slow_refresh),
        patch.object(type(manager._bluetooth_adapters), "adapters", failing_adapters),
    ):
        leader = asyncio.create_task(manager._async_refresh_adapters())
        await refresh_started.wait()
        waiter = asyncio.create_task(manager._async_refresh_adapters())
        await asyncio.sleep(0)
        release_refresh.set()
        with pytest.raises(RuntimeError, match="adapters boom"):
            await leader
        with pytest.raises(RuntimeError, match="adapters boom"):
            await waiter
    assert manager._adapter_refresh_future is None


@pytest.mark.asyncio
async def test_async_refresh_adapters_recovers_after_prior_failure() -> None:
    """Sequential call after a failed refresh must start fresh and succeed."""
    manager = get_manager()
    assert manager._bluetooth_adapters is not None
    call_count = 0

    async def flaky_refresh() -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            msg = "first boom"
            raise RuntimeError(msg)

    with patch.object(manager._bluetooth_adapters, "refresh", new=flaky_refresh):
        with pytest.raises(RuntimeError, match="first boom"):
            await manager._async_refresh_adapters()
        assert manager._adapter_refresh_future is None
        # Second call must start a fresh refresh, not reuse stale future state.
        await manager._async_refresh_adapters()
    assert call_count == 2
    assert manager._adapter_refresh_future is None


@pytest.mark.asyncio
async def test_address_reachability_diagnostics_connectable() -> None:
    """A connectable device in range reports its connectable scanner."""
    manager = get_manager()
    address = "44:44:33:11:23:45"
    scanner = InjectableRemoteScanner(
        "AA:BB:CC:DD:EE:FF", "Living Room Proxy", None, True
    )
    cancel = manager.async_register_scanner(scanner)
    device = generate_ble_device(address, "wohand")
    adv = generate_advertisement_data(local_name="wohand", rssi=-50)
    scanner.inject_advertisement(device, adv)

    diag = manager.async_address_reachability_diagnostics(
        address, BluetoothReachabilityIntent.CONNECTION
    )
    # The address is intentionally not embedded; callers already have it.
    assert address not in diag
    assert "in connectable history" in diag
    assert "1 scanner(s) registered, 1 scanning, 1 connectable" in diag
    assert "Living Room Proxy (AA:BB:CC:DD:EE:FF) (connectable=True, rssi=-50" in diag
    # The "via" source resolves to the scanner name rather than a bare address.
    assert "last advertisement" in diag
    assert "via Living Room Proxy (AA:BB:CC:DD:EE:FF)" in diag
    cancel()


@pytest.mark.asyncio
async def test_address_reachability_diagnostics_non_connectable_only() -> None:
    """A device only seen by a non-connectable scanner has no connectable path."""
    manager = get_manager()
    address = "44:44:33:11:23:46"
    connectable = InjectableRemoteScanner("hci0", "hci0", None, True)
    cancel_c = manager.async_register_scanner(connectable)
    non_connectable = InjectableRemoteScanner("proxy", "proxy", None, False)
    cancel_n = manager.async_register_scanner(non_connectable)
    device = generate_ble_device(address, "wohand")
    adv = generate_advertisement_data(local_name="wohand", rssi=-70)
    non_connectable.inject_advertisement(device, adv)

    diag = manager.async_address_reachability_diagnostics(
        address, BluetoothReachabilityIntent.CONNECTION
    )
    assert "only in non-connectable history (no connectable path)" in diag
    assert "seen by 1 scanner(s) but none with a connectable path" in diag
    assert "proxy (connectable=False, rssi=-70" in diag
    cancel_c()
    cancel_n()


@pytest.mark.asyncio
async def test_address_reachability_diagnostics_advertisement_intent() -> None:
    """An advertisement intent ignores connectable paths and slots."""
    manager = get_manager()
    address = "44:44:33:11:23:4a"
    non_connectable = InjectableRemoteScanner("proxy", "proxy", None, False)
    cancel = manager.async_register_scanner(non_connectable)
    device = generate_ble_device(address, "wohand")
    adv = generate_advertisement_data(local_name="wohand", rssi=-70)
    non_connectable.inject_advertisement(device, adv)

    diag = manager.async_address_reachability_diagnostics(
        address, BluetoothReachabilityIntent.PASSIVE_ADVERTISEMENT
    )
    assert "advertising, seen by 1 scanner(s)" in diag
    assert "no connectable path" not in diag
    assert "slots" not in diag
    # ACTIVE_ADVERTISEMENT is treated the same as PASSIVE_ADVERTISEMENT for now.
    assert diag == manager.async_address_reachability_diagnostics(
        address, BluetoothReachabilityIntent.ACTIVE_ADVERTISEMENT
    )
    cancel()


@pytest.mark.parametrize(
    "intent",
    [
        BluetoothReachabilityIntent.CONNECTION,
        BluetoothReachabilityIntent.PASSIVE_ADVERTISEMENT,
        BluetoothReachabilityIntent.ACTIVE_ADVERTISEMENT,
    ],
)
@pytest.mark.asyncio
async def test_address_reachability_diagnostics_unknown(
    intent: BluetoothReachabilityIntent,
) -> None:
    """An address never seen reports as unknown for every intent."""
    manager = get_manager()
    diag = manager.async_address_reachability_diagnostics("44:44:33:11:23:47", intent)
    assert "unknown (never seen by any scanner)" in diag


@pytest.mark.asyncio
async def test_address_reachability_diagnostics_no_connectable_scanners() -> None:
    """With only a non-connectable scanner the connectable count is zero."""
    manager = get_manager()
    address = "44:44:33:11:23:48"
    non_connectable = InjectableRemoteScanner("proxy", "proxy", None, False)
    cancel = manager.async_register_scanner(non_connectable)
    device = generate_ble_device(address, "wohand")
    adv = generate_advertisement_data(local_name="wohand", rssi=-70)
    non_connectable.inject_advertisement(device, adv)

    diag = manager.async_address_reachability_diagnostics(
        address, BluetoothReachabilityIntent.CONNECTION
    )
    assert "1 scanner(s) registered, 1 scanning, 0 connectable" in diag
    cancel()


@pytest.mark.asyncio
async def test_address_reachability_diagnostics_out_of_slots() -> None:
    """A connectable scanner with no free slots is reported as full."""
    manager = get_manager()
    address = "44:44:33:11:23:49"
    scanner = InjectableRemoteScanner("esphome_proxy", "esphome_proxy", None, True)
    cancel = manager.async_register_scanner(scanner)
    device = generate_ble_device(address, "wohand")
    adv = generate_advertisement_data(local_name="wohand", rssi=-50)
    scanner.inject_advertisement(device, adv)

    with patch.object(
        scanner,
        "get_allocations",
        return_value=Allocations("esphome_proxy", 3, 0, []),
    ):
        diag = manager.async_address_reachability_diagnostics(
            address, BluetoothReachabilityIntent.CONNECTION
        )
    assert "connectable scanner(s) that report slot allocations are all full" in diag
    assert "slots=0/3" in diag
    cancel()


@pytest.mark.asyncio
async def test_address_reachability_diagnostics_in_history_no_scanner() -> None:
    """An address in history but cached by no scanner is not called advertising."""
    manager = get_manager()
    address = "44:44:33:11:23:4c"
    device = generate_ble_device(address, "wohand")
    adv = generate_advertisement_data(local_name="wohand", rssi=-70)
    # Injected from a source with no registered scanner; lands in history but
    # no scanner currently has it cached.
    inject_advertisement_with_source(device, adv, "ghost")

    diag = manager.async_address_reachability_diagnostics(
        address, BluetoothReachabilityIntent.PASSIVE_ADVERTISEMENT
    )
    assert "previously seen but no scanner currently has it cached" in diag
    assert "advertising" not in diag


@pytest.mark.asyncio
async def test_address_reachability_diagnostics_all_scanners_connecting() -> None:
    """When every scanner is paused connecting, the device cannot be seen."""
    manager = get_manager()
    address = "44:44:33:11:23:4b"
    scanner = InjectableRemoteScanner("esphome_proxy", "esphome_proxy", None, True)
    cancel = manager.async_register_scanner(scanner)

    with scanner.connecting():
        assert scanner.scanning is False
        diag = manager.async_address_reachability_diagnostics(
            address, BluetoothReachabilityIntent.CONNECTION
        )
    assert "1 scanner(s) registered, 0 scanning, 1 connectable" in diag
    assert "1 paused while connecting" in diag
    assert "no scanner is currently scanning" in diag
    assert "add more Bluetooth adapters or proxies" in diag
    cancel()


@pytest.mark.asyncio
async def test_address_reachability_diagnostics_scanner_stopped_not_connecting() -> (
    None
):
    """A stopped scanner (not connecting) reports no scanning without the advice."""
    manager = get_manager()
    scanner = InjectableRemoteScanner("esphome_proxy", "esphome_proxy", None, True)
    cancel = manager.async_register_scanner(scanner)
    scanner.scanning = False

    diag = manager.async_address_reachability_diagnostics(
        "44:44:33:11:23:4d", BluetoothReachabilityIntent.CONNECTION
    )
    assert "1 scanner(s) registered, 0 scanning, 1 connectable" in diag
    assert "no scanner is currently scanning" in diag
    assert "paused while connecting" not in diag
    assert "add more Bluetooth adapters or proxies" not in diag
    cancel()


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_bluetooth")
async def test_bleak_callback_exception_is_logged_and_isolated(
    register_hci0_scanner: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A raising bleak callback is caught and logged; siblings still fire."""
    manager = get_manager()
    address = "44:44:33:11:23:01"
    device = generate_ble_device(address, "wohand")
    adv = generate_advertisement_data(local_name="wohand", service_uuids=[], rssi=-40)

    received: list[Any] = []

    def _failing(_device: Any, _adv: Any) -> None:
        msg = "boom"
        raise ValueError(msg)

    def _ok(_device: Any, _adv: Any) -> None:
        received.append(_device)

    cancel_fail = manager.async_register_bleak_callback(_failing, {})
    cancel_ok = manager.async_register_bleak_callback(_ok, {})
    try:
        # A single advertisement dispatch fans out to both callbacks in one
        # loop; the failing one must not stop the ok one from firing.
        inject_advertisement_with_source(device, adv, "hci0")
        assert received  # the ok callback fired despite the sibling raising
        assert "Error in callback" in caplog.text
    finally:
        cancel_fail()
        cancel_ok()


@pytest.mark.asyncio
async def test_supports_passive_scan_reflects_adapter_capability() -> None:
    """supports_passive_scan is True iff any adapter advertises passive scan."""
    manager = BluetoothManager(FakeBluetoothAdapters(), Mock())
    manager._adapters = {"hci0": {ADAPTER_PASSIVE_SCAN: False}}
    assert manager.supports_passive_scan is False
    manager._adapters = {
        "hci0": {ADAPTER_PASSIVE_SCAN: False},
        "hci1": {ADAPTER_PASSIVE_SCAN: True},
    }
    assert manager.supports_passive_scan is True


@pytest.mark.asyncio
async def test_get_bluetooth_adapters_cached_with_empty_cache() -> None:
    """cached=True still populates when the adapter cache is empty (no refresh)."""
    adapters = FakeBluetoothAdapters()
    manager = BluetoothManager(adapters, Mock())
    with patch("habluetooth.manager.IS_LINUX", False):
        await manager.async_setup()
    try:
        manager._adapters = {}
        # cached=True with an empty cache repopulates straight from the backend
        # without taking the refresh path.
        with patch.object(adapters, "refresh", wraps=adapters.refresh) as spy:
            result = await manager.async_get_bluetooth_adapters(cached=True)
        spy.assert_not_called()
        assert result == adapters.adapters
    finally:
        manager.async_stop()


@pytest.mark.asyncio
async def test_get_adapter_from_address_refreshes_when_not_found() -> None:
    """A miss triggers a refresh, then a second lookup."""
    adapters = FakeBluetoothAdapters()
    manager = BluetoothManager(adapters, Mock())
    with patch("habluetooth.manager.IS_LINUX", False):
        await manager.async_setup()
    try:
        manager._adapters = {}
        # Unknown address: first lookup misses, a refresh runs, second lookup
        # still misses against the empty fake backend.
        with patch.object(adapters, "refresh", wraps=adapters.refresh) as spy:
            assert (
                await manager.async_get_adapter_from_address("00:00:00:00:00:09")
                is None
            )
        spy.assert_called_once()  # the miss forced a refresh

        # Known address resolves on the first lookup.
        manager._adapters = {"hci7": {ADAPTER_ADDRESS: "00:00:00:00:00:07"}}
        assert (
            await manager.async_get_adapter_from_address("00:00:00:00:00:07") == "hci7"
        )
    finally:
        manager.async_stop()


@pytest.mark.asyncio
async def test_async_setup_assigns_central_manager_when_unset() -> None:
    """async_setup claims the central singleton when it is unset."""
    original = CentralBluetoothManager.manager
    manager = BluetoothManager(FakeBluetoothAdapters(), Mock())
    try:
        CentralBluetoothManager.manager = None
        with patch("habluetooth.manager.IS_LINUX", False):
            await manager.async_setup()
        assert CentralBluetoothManager.manager is manager
    finally:
        CentralBluetoothManager.manager = original
        manager.async_stop()


@pytest.mark.asyncio
async def test_async_setup_returns_early_on_non_linux() -> None:
    """On non-Linux, setup skips mgmt control entirely."""
    manager = BluetoothManager(FakeBluetoothAdapters(), Mock())
    with patch("habluetooth.manager.IS_LINUX", False):
        await manager.async_setup()
        # Inside the non-Linux patch, setup returned before touching mgmt.
        assert manager._mgmt_ctl is None
        assert manager.is_operating_degraded() is False
    manager.async_stop()


@pytest.mark.asyncio
async def test_async_setup_handles_connection_error() -> None:
    """A CONNECTION_ERRORS failure during mgmt setup degrades gracefully."""
    manager = BluetoothManager(FakeBluetoothAdapters(), Mock())
    with (
        patch("habluetooth.manager.IS_LINUX", True),
        patch("habluetooth.manager.MGMTBluetoothCtl") as mock_mgmt_class,
    ):
        mock_instance = Mock()
        mock_instance.setup = AsyncMock(side_effect=OSError("no socket"))
        mock_mgmt_class.return_value = mock_instance
        await manager.async_setup()
    try:
        assert manager._mgmt_ctl is None
        assert manager.has_advertising_side_channel is False
    finally:
        manager.async_stop()


@pytest.mark.asyncio
async def test_async_stop_without_unavailable_tracking() -> None:
    """async_stop is a no-op for unavailable tracking when none is scheduled."""
    manager = BluetoothManager(FakeBluetoothAdapters(), Mock())
    with patch("habluetooth.manager.IS_LINUX", False):
        await manager.async_setup()
    manager._cancel_unavailable_tracking = None
    # Should not raise even though there is no tracking handle to cancel.
    manager.async_stop()
    assert manager.shutdown is True


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_bluetooth")
async def test_unavailable_callback_exception_isolated(
    register_hci0_scanner: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A raising unavailable callback is logged; a sibling still fires."""
    manager = get_manager()
    address = "44:44:33:11:23:02"
    start = time.monotonic()
    device = generate_ble_device(address, "wohand")
    adv = generate_advertisement_data(local_name="wohand", service_uuids=[], rssi=-60)
    inject_advertisement_with_time_and_source_connectable(
        device, adv, start, HCI0_SOURCE_ADDRESS, False
    )

    ok_calls: list[Any] = []

    def _failing(_info: BluetoothServiceInfoBleak) -> None:
        msg = "boom"
        raise ValueError(msg)

    def _ok(_info: BluetoothServiceInfoBleak) -> None:
        ok_calls.append(_info)

    cancel_fail = manager.async_track_unavailable(_failing, address, connectable=False)
    cancel_ok = manager.async_track_unavailable(_ok, address, connectable=False)
    try:
        # Push the clock well past the fallback staleness window so the device
        # is considered unavailable.
        with patch_bluetooth_time(start + 100_000):
            manager._async_check_unavailable()
        assert len(ok_calls) == 1
        assert "Error in unavailable callback" in caplog.text
    finally:
        cancel_fail()
        cancel_ok()


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_bluetooth")
async def test_remove_unavailable_callback_keeps_siblings(
    register_hci0_scanner: None,
) -> None:
    """Cancelling one unavailable callback leaves the address entry in place."""
    manager = get_manager()
    address = "44:44:33:11:23:03"

    def _cb_a(_info: BluetoothServiceInfoBleak) -> None:
        return

    def _cb_b(_info: BluetoothServiceInfoBleak) -> None:
        return

    cancel_a = manager.async_track_unavailable(_cb_a, address, connectable=False)
    cancel_b = manager.async_track_unavailable(_cb_b, address, connectable=False)
    try:
        cancel_a()
        # One callback remains, so the address bucket is not deleted.
        assert address in manager._unavailable_callbacks
        assert _cb_b in manager._unavailable_callbacks[address]
    finally:
        cancel_b()
    assert address not in manager._unavailable_callbacks


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_bluetooth")
async def test_unregister_source_callback_keeps_siblings(
    register_hci0_scanner: None,
) -> None:
    """Cancelling one source-keyed callback leaves the source bucket in place."""
    manager = get_manager()

    def _cb_a(_change: HaScannerModeChange) -> None:
        return

    def _cb_b(_change: HaScannerModeChange) -> None:
        return

    cancel_a = manager.async_register_scanner_mode_change_callback(_cb_a, None)
    cancel_b = manager.async_register_scanner_mode_change_callback(_cb_b, None)
    try:
        cancel_a()
        # One callback remains under the None source, so it is not deleted.
        assert None in manager._scanner_mode_change_callbacks
        assert _cb_b in manager._scanner_mode_change_callbacks[None]
    finally:
        cancel_b()
    assert None not in manager._scanner_mode_change_callbacks
    # Cancelling again once the source bucket is gone is a no-op.
    cancel_b()
    assert None not in manager._scanner_mode_change_callbacks


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_bluetooth")
async def test_should_keep_previous_adv_logs_when_debug_enabled(
    register_hci0_scanner: None,
    register_hci1_scanner: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """With debug on, the keep-previous decision logs its switch reasons."""
    manager = get_manager()
    manager._debug = True
    address = "44:44:33:11:23:04"
    device = generate_ble_device(address, "wohand")

    start = time.monotonic()
    weak = generate_advertisement_data(local_name="wohand", service_uuids=[], rssi=-40)
    inject_advertisement_with_time_and_source(device, weak, start, HCI0_SOURCE_ADDRESS)

    with caplog.at_level(logging.DEBUG, logger="habluetooth.manager"):
        # A clearly stronger reading from a second still-scanning source wins
        # on RSSI (RSSI-switch debug branch).
        strong = generate_advertisement_data(
            local_name="wohand", service_uuids=[], rssi=-20
        )
        inject_advertisement_with_time_and_source(
            device, strong, start + 1, HCI1_SOURCE_ADDRESS
        )
        assert "new rssi" in caplog.text

        caplog.clear()
        # A far-future reading makes the previous one stale, so any new
        # advertisement wins regardless of RSSI (stale-switch debug branch).
        inject_advertisement_with_time_and_source(
            device, weak, start + 100_000, HCI0_SOURCE_ADDRESS
        )
        assert "time elapsed" in caplog.text


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_bluetooth")
async def test_non_connectable_advertisement_rejected_in_favour_of_previous(
    register_hci0_scanner: None,
    register_hci1_scanner: None,
) -> None:
    """A weaker non-connectable reading is rejected without re-adding history."""
    manager = get_manager()
    address = "44:44:33:11:23:05"
    device = generate_ble_device(address, "wohand")
    start = time.monotonic()

    strong = generate_advertisement_data(
        local_name="wohand", service_uuids=[], rssi=-30
    )
    inject_advertisement_with_time_and_source_connectable(
        device, strong, start, HCI0_SOURCE_ADDRESS, False
    )

    # A weaker, non-connectable reading from a second still-scanning source is
    # rejected; the stronger hci0 reading stays in history.
    weak = generate_advertisement_data(local_name="wohand", service_uuids=[], rssi=-95)
    inject_advertisement_with_time_and_source_connectable(
        device, weak, start + 1, HCI1_SOURCE_ADDRESS, False
    )

    kept = manager.async_last_service_info(address, connectable=False)
    assert kept is not None
    assert kept.source == HCI0_SOURCE_ADDRESS
    assert kept.rssi == -30


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_bluetooth")
async def test_should_keep_previous_adv_switches_without_debug_logging(
    register_hci0_scanner: None,
    register_hci1_scanner: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Switch decisions are silent when debug logging is disabled."""
    manager = get_manager()
    manager._debug = False
    address = "44:44:33:11:23:06"
    device = generate_ble_device(address, "wohand")
    start = time.monotonic()

    weak = generate_advertisement_data(local_name="wohand", service_uuids=[], rssi=-40)
    inject_advertisement_with_time_and_source(device, weak, start, HCI0_SOURCE_ADDRESS)

    with caplog.at_level(logging.DEBUG, logger="habluetooth.manager"):
        # RSSI switch: a stronger second source wins.
        strong = generate_advertisement_data(
            local_name="wohand", service_uuids=[], rssi=-20
        )
        inject_advertisement_with_time_and_source(
            device, strong, start + 1, HCI1_SOURCE_ADDRESS
        )
        # Stale switch: a far-future reading wins.
        inject_advertisement_with_time_and_source(
            device, weak, start + 100_000, HCI0_SOURCE_ADDRESS
        )

    # The switch happened, but nothing was logged.
    latest = manager.async_last_service_info(address, connectable=True)
    assert latest is not None
    assert latest.source == HCI0_SOURCE_ADDRESS
    assert "Switching from" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_bluetooth")
async def test_non_connectable_adv_promoted_when_connectable_path_registered(
    register_hci0_scanner: None,
) -> None:
    """
    A changed non-connectable adv is promoted when a connectable path is live.

    Regression test for #534: a connectable scanner has a path to the device, but
    the current best advertisement arrives from a non-connectable source. The
    service_info must surface as connectable so connectable callbacks and discovery
    fire, otherwise Home Assistant believes there is no connectable path.
    """
    manager = get_manager()
    address = "44:44:33:11:23:45"
    now = time.monotonic()

    discovered: list[BluetoothServiceInfoBleak] = []
    manager._subclass_discover_info = Mock(side_effect=discovered.append)
    bleak_devices: list[BLEDevice] = []

    def _on_bleak(dev: BLEDevice, _adv: AdvertisementData) -> None:
        bleak_devices.append(dev)

    # Register up front so the connectable_history replay on registration cannot
    # be mistaken for a promotion dispatch.
    cancel = manager.async_register_bleak_callback(_on_bleak, {})
    try:
        # Connectable adv from the registered hci0 scanner populates
        # connectable_history.
        device = generate_ble_device(address, "wohand")
        connectable_adv = generate_advertisement_data(
            local_name="wohand",
            service_uuids=[],
            manufacturer_data={1: b"\x01"},
            rssi=-60,
        )
        inject_advertisement_with_time_and_source_connectable(
            device, connectable_adv, now, HCI0_SOURCE_ADDRESS, True
        )

        discovered.clear()
        bleak_devices.clear()

        # A stronger non-connectable adv from another source wins the best-path
        # comparison and carries changed data so the identical-adv short-circuit
        # does not skip dispatch.
        non_connectable_adv = generate_advertisement_data(
            local_name="wohand",
            service_uuids=[],
            manufacturer_data={1: b"\x02"},
            rssi=-20,
        )
        inject_advertisement_with_time_and_source_connectable(
            device,
            non_connectable_adv,
            now,
            NON_CONNECTABLE_REMOTE_SOURCE_ADDRESS,
            False,
        )
    finally:
        cancel()

    assert discovered
    assert discovered[-1].connectable is True
    assert bleak_devices


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_bluetooth")
async def test_non_connectable_adv_not_promoted_after_connectable_scanner_unregisters() -> (  # noqa: E501
    None
):
    """
    A lingering connectable_history entry does not promote once its source is gone.

    connectable_history is only pruned by the periodic unavailable check, so an
    unregistered connectable scanner can leave a stale entry behind. The promotion
    must verify the stored source is still registered before claiming a live path.
    """
    manager = get_manager()
    address = "44:44:33:11:23:46"
    now = time.monotonic()

    discovered: list[BluetoothServiceInfoBleak] = []
    manager._subclass_discover_info = Mock(side_effect=discovered.append)
    bleak_devices: list[BLEDevice] = []

    def _on_bleak(dev: BLEDevice, _adv: AdvertisementData) -> None:
        bleak_devices.append(dev)

    # Register up front so the connectable_history replay on registration cannot
    # be mistaken for a promotion dispatch.
    cancel = manager.async_register_bleak_callback(_on_bleak, {})
    try:
        connectable_scanner = FakeScanner(HCI0_SOURCE_ADDRESS, "hci0")
        connectable_scanner.connectable = True
        cancel_scanner = manager.async_register_scanner(connectable_scanner)

        device = generate_ble_device(address, "wohand")
        connectable_adv = generate_advertisement_data(
            local_name="wohand",
            service_uuids=[],
            manufacturer_data={1: b"\x01"},
            rssi=-60,
        )
        inject_advertisement_with_time_and_source_connectable(
            device, connectable_adv, now, HCI0_SOURCE_ADDRESS, True
        )

        # Unregister the only connectable scanner; the connectable_history entry
        # intentionally lingers since unregister does not prune it.
        cancel_scanner()
        assert HCI0_SOURCE_ADDRESS not in manager._sources
        assert address in manager._connectable_history

        discovered.clear()
        bleak_devices.clear()

        non_connectable_adv = generate_advertisement_data(
            local_name="wohand",
            service_uuids=[],
            manufacturer_data={1: b"\x02"},
            rssi=-20,
        )
        inject_advertisement_with_time_and_source_connectable(
            device,
            non_connectable_adv,
            now,
            NON_CONNECTABLE_REMOTE_SOURCE_ADDRESS,
            False,
        )
    finally:
        cancel()

    assert discovered
    assert discovered[-1].connectable is False
    assert not bleak_devices


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_bluetooth")
async def test_connectable_adv_still_dispatches_to_bleak_callbacks(
    register_hci0_scanner: None,
) -> None:
    """A normal connectable adv still dispatches to bleak callbacks unchanged."""
    manager = get_manager()
    address = "44:44:33:11:23:47"
    device = generate_ble_device(address, "wohand")
    adv = generate_advertisement_data(local_name="wohand", service_uuids=[], rssi=-40)

    bleak_devices: list[BLEDevice] = []

    def _on_bleak(dev: BLEDevice, _adv: AdvertisementData) -> None:
        bleak_devices.append(dev)

    cancel = manager.async_register_bleak_callback(_on_bleak, {})
    try:
        inject_advertisement_with_time_and_source_connectable(
            device, adv, time.monotonic(), HCI0_SOURCE_ADDRESS, True
        )
    finally:
        cancel()

    assert bleak_devices == [device]
