"""Tests for the mgmt-based local scanner and the create_local_scanner factory."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from bluetooth_adapters import DEFAULT_ADDRESS
from bluetooth_data_tools import monotonic_time_coarse

from habluetooth import (
    BluetoothScanningMode,
    HaScanner,
    HaScannerMgmt,
    create_local_scanner,
    get_manager,
)
from habluetooth.scanner_bleak import ScannerStartError

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.asyncio

_ADAPTER = "hci0"
_ADAPTER_IDX = 0
_ADDRESS = "00:11:22:33:44:55"
_PEER = "AA:BB:CC:DD:EE:FF"


class FakeMgmt:
    """Stand-in for MGMTBluetoothCtl recording the discovery calls made."""

    def __init__(self, *, can_discover: bool = True) -> None:
        self.can_discover = can_discover
        self.started: list[int] = []
        self.stopped: list[int] = []
        self.monitors_added: list[int] = []
        self.monitors_removed: list[tuple[int, int]] = []
        self.start_ok = True
        self.stop_ok = True
        self.remove_ok = True
        self.monitor_handle: int | None = 7

    async def start_discovery(self, idx: int) -> bool:
        self.started.append(idx)
        return self.start_ok

    async def stop_discovery(self, idx: int) -> bool:
        self.stopped.append(idx)
        return self.stop_ok

    async def add_adv_pattern_monitor(self, idx: int) -> int | None:
        self.monitors_added.append(idx)
        return self.monitor_handle

    async def remove_adv_monitor(self, idx: int, handle: int) -> bool:
        self.monitors_removed.append((idx, handle))
        return self.remove_ok


@pytest.fixture
def use_mgmt() -> Iterator[FakeMgmt]:
    """Point the manager at a fake mgmt controller that can discover."""
    fake = FakeMgmt()
    with patch.object(get_manager(), "get_bluez_mgmt_ctl", return_value=fake):
        yield fake


def _scanner(
    mode: BluetoothScanningMode = BluetoothScanningMode.ACTIVE,
) -> HaScannerMgmt:
    return HaScannerMgmt(mode, _ADAPTER, _ADDRESS)


# -- factory --------------------------------------------------------------
async def test_factory_falls_back_without_mgmt() -> None:
    """With no mgmt controller, the factory returns the bleak scanner."""
    with patch.object(get_manager(), "get_bluez_mgmt_ctl", return_value=None):
        scanner = create_local_scanner(BluetoothScanningMode.ACTIVE, _ADAPTER, _ADDRESS)
    assert type(scanner) is HaScanner


async def test_factory_falls_back_when_cannot_discover() -> None:
    """A mgmt controller without discovery capability still uses bleak."""
    fake = FakeMgmt(can_discover=False)
    with (
        patch("habluetooth.scanner_mgmt.IS_LINUX", True),
        patch.object(get_manager(), "get_bluez_mgmt_ctl", return_value=fake),
    ):
        scanner = create_local_scanner(BluetoothScanningMode.ACTIVE, _ADAPTER, _ADDRESS)
    assert type(scanner) is HaScanner


async def test_factory_falls_back_for_non_hci_adapter(use_mgmt: FakeMgmt) -> None:
    """A non-hci adapter has no controller index, so it uses bleak."""
    with patch("habluetooth.scanner_mgmt.IS_LINUX", True):
        scanner = create_local_scanner(
            BluetoothScanningMode.ACTIVE, "CoreBluetooth", _ADDRESS
        )
    assert type(scanner) is HaScanner


async def test_factory_falls_back_without_real_address(use_mgmt: FakeMgmt) -> None:
    """Without a real adapter BD_ADDR the mgmt scanner cannot pin connects."""
    with patch("habluetooth.scanner_mgmt.IS_LINUX", True):
        scanner = create_local_scanner(
            BluetoothScanningMode.ACTIVE, _ADAPTER, DEFAULT_ADDRESS
        )
    assert type(scanner) is HaScanner


async def test_factory_falls_back_for_auto_mode(use_mgmt: FakeMgmt) -> None:
    """AUTO needs active-window promotion the mgmt scanner lacks, so use bleak."""
    with patch("habluetooth.scanner_mgmt.IS_LINUX", True):
        scanner = create_local_scanner(BluetoothScanningMode.AUTO, _ADAPTER, _ADDRESS)
    assert type(scanner) is HaScanner


async def test_factory_returns_mgmt_scanner_when_available(
    use_mgmt: FakeMgmt,
) -> None:
    """On Linux with a discovering mgmt socket and an hci adapter, use mgmt."""
    with patch("habluetooth.scanner_mgmt.IS_LINUX", True):
        scanner = create_local_scanner(BluetoothScanningMode.ACTIVE, _ADAPTER, _ADDRESS)
    assert type(scanner) is HaScannerMgmt


async def test_init_rejects_default_address() -> None:
    """Direct construction without a real BD_ADDR fails fast."""
    with pytest.raises(ValueError, match="real adapter address"):
        HaScannerMgmt(BluetoothScanningMode.ACTIVE, _ADAPTER, DEFAULT_ADDRESS)


# -- construction ---------------------------------------------------------
async def test_init_is_connectable_with_connector() -> None:
    """The scanner is connectable and routes through a mgmt connector."""
    scanner = _scanner()
    assert scanner.connectable is True
    assert scanner.connector is not None
    assert scanner.connector.source == _ADDRESS
    assert scanner.connector.can_connect == scanner._can_connect
    # The connect path derives the backend id from this; it should read as the
    # client, not the generic "partial".
    assert type(scanner.connector.client).__name__ == "HaMgmtClient"


# -- discovery lifecycle --------------------------------------------------
async def test_async_start_active_uses_start_discovery(use_mgmt: FakeMgmt) -> None:
    """Active mode starts kernel discovery and marks the scanner scanning."""
    scanner = _scanner(BluetoothScanningMode.ACTIVE)
    scanner.async_setup()
    await scanner.async_start()
    assert use_mgmt.started == [_ADAPTER_IDX]
    assert scanner.scanning is True
    assert scanner.current_mode is BluetoothScanningMode.ACTIVE
    await scanner.async_stop()


async def test_start_notifies_mode_change(use_mgmt: FakeMgmt) -> None:
    """Starting sets the radio mode through the manager notification path."""
    scanner = _scanner(BluetoothScanningMode.ACTIVE)
    scanner.async_setup()
    with patch.object(get_manager(), "scanner_mode_changed") as notify:
        await scanner.async_start()
    notify.assert_called_once_with(scanner)
    await scanner.async_stop()


async def test_async_start_passive_uses_monitor(use_mgmt: FakeMgmt) -> None:
    """Passive mode adds an advertisement monitor and records its handle."""
    scanner = _scanner(BluetoothScanningMode.PASSIVE)
    scanner.async_setup()
    await scanner.async_start()
    assert use_mgmt.monitors_added == [_ADAPTER_IDX]
    assert scanner._monitor_handle == 7
    assert scanner.scanning is True
    await scanner.async_stop()


async def test_async_start_raises_when_unavailable() -> None:
    """Starting without mgmt discovery raises ScannerStartError."""
    scanner = _scanner()
    scanner.async_setup()
    with (
        patch.object(get_manager(), "get_bluez_mgmt_ctl", return_value=None),
        pytest.raises(ScannerStartError, match="not available"),
    ):
        await scanner.async_start()


async def test_async_start_raises_when_discovery_fails(use_mgmt: FakeMgmt) -> None:
    """A rejected start_discovery surfaces as ScannerStartError."""
    use_mgmt.start_ok = False
    scanner = _scanner(BluetoothScanningMode.ACTIVE)
    scanner.async_setup()
    with pytest.raises(ScannerStartError, match="failed to start"):
        await scanner.async_start()


async def test_async_start_raises_when_monitor_fails(use_mgmt: FakeMgmt) -> None:
    """A rejected monitor registration surfaces as ScannerStartError."""
    use_mgmt.monitor_handle = None
    scanner = _scanner(BluetoothScanningMode.PASSIVE)
    scanner.async_setup()
    with pytest.raises(ScannerStartError, match="advertisement monitor"):
        await scanner.async_start()


async def test_async_stop_active_stops_discovery(use_mgmt: FakeMgmt) -> None:
    """Stopping an active scanner stops kernel discovery."""
    scanner = _scanner(BluetoothScanningMode.ACTIVE)
    scanner.async_setup()
    await scanner.async_start()
    await scanner.async_stop()
    assert use_mgmt.stopped == [_ADAPTER_IDX]
    assert scanner.scanning is False


async def test_async_stop_passive_removes_monitor(use_mgmt: FakeMgmt) -> None:
    """Stopping a passive scanner removes its advertisement monitor."""
    scanner = _scanner(BluetoothScanningMode.PASSIVE)
    scanner.async_setup()
    await scanner.async_start()
    await scanner.async_stop()
    assert use_mgmt.monitors_removed == [(_ADAPTER_IDX, 7)]
    assert scanner._monitor_handle is None


# -- connection slots -----------------------------------------------------
async def test_can_connect_respects_slot_limit() -> None:
    """can_connect tracks live connections against the configured slot count."""
    scanner = _scanner()
    get_manager().slot_manager.register_adapter(_ADAPTER, 1)
    assert scanner._can_connect() is True
    scanner._register_connection(_PEER)
    assert scanner._can_connect() is False
    scanner._unregister_connection(_PEER)
    assert scanner._can_connect() is True


async def test_can_connect_unlimited_without_registered_slots() -> None:
    """With no slot count registered, connections are not slot-gated."""
    scanner = _scanner()
    assert scanner._can_connect() is True
    scanner._register_connection(_PEER)
    assert scanner._can_connect() is True


async def test_can_connect_false_when_not_connectable() -> None:
    """A non-connectable scanner never reports it can connect."""
    scanner = _scanner()
    scanner.connectable = False
    assert scanner._can_connect() is False


async def test_get_allocations_reflects_tracked_connections() -> None:
    """get_allocations reports free/allocated from the scanner's own tracking."""
    scanner = _scanner()
    assert scanner.get_allocations() is None  # no slots registered yet
    get_manager().slot_manager.register_adapter(_ADAPTER, 3)
    scanner._register_connection(_PEER)
    allocations = scanner.get_allocations()
    assert allocations is not None
    assert allocations.slots == 3
    assert allocations.free == 2
    assert allocations.allocated == [_PEER]


async def test_get_allocations_clamps_free_at_zero() -> None:
    """An overshoot past the advisory gate reports zero free, never negative."""
    scanner = _scanner()
    get_manager().slot_manager.register_adapter(_ADAPTER, 1)
    scanner._register_connection(_PEER)
    scanner._register_connection("11:22:33:44:55:66")  # overshoot the 1 slot
    allocations = scanner.get_allocations()
    assert allocations is not None
    assert allocations.free == 0


async def test_get_allocations_sorts_allocated() -> None:
    """Allocated addresses are reported in a stable sorted order."""
    scanner = _scanner()
    get_manager().slot_manager.register_adapter(_ADAPTER, 5)
    scanner._register_connection("CC:CC:CC:CC:CC:CC")
    scanner._register_connection("AA:AA:AA:AA:AA:AA")
    scanner._register_connection("BB:BB:BB:BB:BB:BB")
    allocations = scanner.get_allocations()
    assert allocations is not None
    assert allocations.allocated == [
        "AA:AA:AA:AA:AA:AA",
        "BB:BB:BB:BB:BB:BB",
        "CC:CC:CC:CC:CC:CC",
    ]


# -- watchdog -------------------------------------------------------------
async def test_watchdog_restarts_discovery_when_quiet(use_mgmt: FakeMgmt) -> None:
    """The watchdog restarts discovery after the adapter goes quiet."""
    scanner = _scanner(BluetoothScanningMode.ACTIVE)
    scanner.async_setup()
    await scanner.async_start()
    assert use_mgmt.started == [_ADAPTER_IDX]
    with patch.object(scanner, "_async_watchdog_triggered", return_value=True):
        scanner._async_scanner_watchdog()
        assert scanner.scanning is False
        # Let the restart background task run to completion.
        for task in list(scanner._background_tasks):
            await task
    assert use_mgmt.stopped == [_ADAPTER_IDX]  # stopped during restart
    assert use_mgmt.started == [_ADAPTER_IDX, _ADAPTER_IDX]  # then started again
    await scanner.async_stop()


async def test_watchdog_noop_when_not_triggered(use_mgmt: FakeMgmt) -> None:
    """The watchdog does nothing while advertisements are still arriving."""
    scanner = _scanner(BluetoothScanningMode.ACTIVE)
    scanner.async_setup()
    await scanner.async_start()
    with patch.object(scanner, "_async_watchdog_triggered", return_value=False):
        scanner._async_scanner_watchdog()
    assert use_mgmt.stopped == []
    assert use_mgmt.started == [_ADAPTER_IDX]
    await scanner.async_stop()


async def test_watchdog_skips_when_restart_in_progress(use_mgmt: FakeMgmt) -> None:
    """A triggered watchdog does not stack a second restart on the lock."""
    scanner = _scanner(BluetoothScanningMode.ACTIVE)
    scanner.async_setup()
    await scanner.async_start()
    async with scanner._start_stop_lock:
        with patch.object(scanner, "_async_watchdog_triggered", return_value=True):
            scanner._async_scanner_watchdog()
        assert scanner._background_tasks == set()  # no restart scheduled
    await scanner.async_stop()


async def test_watchdog_restart_logs_on_failure(
    use_mgmt: FakeMgmt, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed restart is logged, not raised out of the background task."""
    scanner = _scanner(BluetoothScanningMode.ACTIVE)
    scanner.async_setup()
    await scanner.async_start()
    use_mgmt.start_ok = False  # the restart's start_discovery will fail
    with (
        patch.object(scanner, "_async_watchdog_triggered", return_value=True),
        caplog.at_level("ERROR", logger="habluetooth.scanner_mgmt"),
    ):
        scanner._async_scanner_watchdog()
        for task in list(scanner._background_tasks):
            await task
    assert "failed to restart" in caplog.text
    await scanner.async_stop()


async def test_restart_does_not_stack_monitor_on_failed_removal(
    use_mgmt: FakeMgmt,
) -> None:
    """If a passive monitor cannot be removed, the restart does not add another."""
    use_mgmt.remove_ok = False
    scanner = _scanner(BluetoothScanningMode.PASSIVE)
    scanner.async_setup()
    await scanner.async_start()
    assert use_mgmt.monitors_added == [_ADAPTER_IDX]
    with patch.object(scanner, "_async_watchdog_triggered", return_value=True):
        scanner._async_scanner_watchdog()
        for task in list(scanner._background_tasks):
            await task
    # Removal failed, so no second monitor was registered (no stacking/leak),
    # and the handle is preserved for a retry on the next tick.
    assert use_mgmt.monitors_added == [_ADAPTER_IDX]
    assert scanner._monitor_handle == 7
    await scanner.async_stop()


async def test_background_task_failure_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An exception escaping a background task is logged, not lost at GC."""
    scanner = _scanner()

    async def boom() -> None:
        msg = "kaboom"
        raise RuntimeError(msg)

    with caplog.at_level("ERROR", logger="habluetooth.scanner_mgmt"):
        scanner._create_background_task(boom())
        task = next(iter(scanner._background_tasks))
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)  # let the done-callback run
    assert "background task failed" in caplog.text
    assert scanner._background_tasks == set()  # reference dropped


async def test_unsetup_stops_watchdog(use_mgmt: FakeMgmt) -> None:
    """The teardown callback cancels the watchdog timer."""
    scanner = _scanner(BluetoothScanningMode.ACTIVE)
    unsetup = scanner.async_setup()
    await scanner.async_start()
    armed = scanner._cancel_watchdog  # capture before asserting to avoid narrowing
    assert armed is not None  # armed by the start
    unsetup()
    assert scanner._cancel_watchdog is None
    await scanner.async_stop()


async def test_stop_discovery_noop_without_mgmt() -> None:
    """Stopping with no mgmt controller is a safe no-op."""
    scanner = _scanner()
    with patch.object(get_manager(), "get_bluez_mgmt_ctl", return_value=None):
        await scanner._async_stop_discovery()  # does not raise


async def test_stop_active_warns_on_failure(
    use_mgmt: FakeMgmt, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed stop_discovery is logged and the scanner still stops scanning."""
    use_mgmt.stop_ok = False
    scanner = _scanner(BluetoothScanningMode.ACTIVE)
    scanner.async_setup()
    await scanner.async_start()
    with caplog.at_level("WARNING", logger="habluetooth.scanner_mgmt"):
        await scanner.async_stop()
    assert "failed to stop mgmt discovery" in caplog.text
    assert scanner.scanning is False


async def test_stop_passive_keeps_handle_on_failed_removal(
    use_mgmt: FakeMgmt, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed monitor removal keeps the handle so a later stop can retry."""
    use_mgmt.remove_ok = False
    scanner = _scanner(BluetoothScanningMode.PASSIVE)
    scanner.async_setup()
    await scanner.async_start()
    with caplog.at_level("WARNING", logger="habluetooth.scanner_mgmt"):
        await scanner.async_stop()
    assert "failed to remove advertisement monitor" in caplog.text
    assert scanner._monitor_handle == 7  # not cleared, so a retry is possible


async def test_registered_scanner_reports_discovered_devices(
    use_mgmt: FakeMgmt,
) -> None:
    """Once registered, the inherited ingestion feeds the read-out overrides."""
    scanner = _scanner()
    scanner.async_setup()
    unregister = get_manager().async_register_scanner(scanner)
    try:
        assert list(scanner.discovered_addresses) == []
        assert scanner.discovered_devices == []
        assert scanner.discovered_devices_and_advertisement_data == {}
        assert scanner.get_discovered_device_advertisement_data(_PEER) is None
        scanner._async_on_advertisement(
            _PEER, -60, "dev", [], {}, {}, None, {}, monotonic_time_coarse()
        )
        assert _PEER in scanner.discovered_addresses
        assert _PEER in scanner.discovered_devices_and_advertisement_data
        result = scanner.get_discovered_device_advertisement_data(_PEER)
        assert result is not None
        assert result[0].address == _PEER
        assert scanner.discovered_devices[0].address == _PEER
    finally:
        unregister()


async def test_slot_limit_zero_when_adapter_unregistered() -> None:
    """An adapter with no registered slot count reports 0 (unlimited)."""
    scanner = _scanner()
    assert scanner._slot_limit() == 0
    assert scanner._can_connect() is True
