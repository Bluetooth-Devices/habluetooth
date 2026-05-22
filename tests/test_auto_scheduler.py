"""Tests for the auto-mode active-window scheduler."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

import pytest
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from habluetooth import (
    BaseHaScanner,
    BluetoothScanningMode,
    get_manager,
)
from habluetooth.auto_scheduler import ActiveScanRequest
from habluetooth.const import (
    AUTO_INITIAL_SWEEP_DELAY,
    AUTO_REDISCOVERY_INTERVAL,
    AUTO_REDISCOVERY_SWEEP_DURATION,
    AUTO_WINDOW_MAX_DURATION,
    AUTO_WINDOW_MIN_DURATION,
)

from . import generate_advertisement_data, generate_ble_device


class _RecordingAutoScanner(BaseHaScanner):
    """BaseHaScanner subclass that records active-window calls."""

    __slots__ = ("_block_event", "_return_value", "active_window_calls")

    def __init__(
        self,
        source: str,
        mode: BluetoothScanningMode | None,
        connectable: bool = True,
    ) -> None:
        super().__init__(source, source, requested_mode=mode)
        self.connectable = connectable
        self.active_window_calls: list[float] = []
        self._block_event: asyncio.Event | None = None
        self._return_value = True

    async def async_request_active_window(self, duration: float) -> bool:
        self.active_window_calls.append(duration)
        if self._block_event is not None:
            await self._block_event.wait()
        return self._return_value

    @property
    def discovered_devices(self) -> list[BLEDevice]:
        return []

    @property
    def discovered_devices_and_advertisement_data(
        self,
    ) -> dict[str, tuple[BLEDevice, AdvertisementData]]:
        return {}

    def get_discovered_device_advertisement_data(
        self, address: str
    ) -> tuple[BLEDevice, AdvertisementData] | None:
        return None

    @property
    def discovered_addresses(self) -> Iterable[str]:
        return ()


def _inject(scanner: _RecordingAutoScanner, address: str) -> None:
    """Drive a fake advertisement through the scanner's normal path."""
    adv = generate_advertisement_data(local_name="x")
    device = generate_ble_device(address, "x")
    scanner._async_on_advertisement(
        device.address,
        adv.rssi,
        device.name or "",
        adv.service_uuids,
        adv.service_data,
        adv.manufacturer_data,
        adv.tx_power,
        {},
        asyncio.get_running_loop().time(),
    )


async def _drain() -> None:
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_advertisement_starts_tracking() -> None:
    """A matching address advertisement creates a per-(address, request) entry."""
    manager = get_manager()
    sched = manager._auto_scheduler
    cancel = manager.async_register_active_scan(
        "11:22:33:44:55:66", scan_interval=120.0, scan_duration=3.0
    )
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        _inject(scanner, "11:22:33:44:55:66")
        assert "11:22:33:44:55:66" in sched._needs
    finally:
        cancel()
        register_cancel()
    assert sched._needs == {}


@pytest.mark.asyncio
async def test_advertisement_for_unrelated_address_is_ignored() -> None:
    """An advertisement for an unregistered address creates no tracking."""
    manager = get_manager()
    sched = manager._auto_scheduler
    cancel = manager.async_register_active_scan(
        "11:22:33:44:55:66", scan_interval=120.0
    )
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        _inject(scanner, "AA:AA:AA:AA:AA:AA")
        assert sched._needs == {}
    finally:
        cancel()
        register_cancel()


@pytest.mark.asyncio
async def test_tick_requests_active_window_on_auto_scanner() -> None:
    """A due tracker entry triggers an active window on the owning scanner."""
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    cancel = manager.async_register_active_scan(
        "11:22:33:44:55:66", scan_interval=120.0, scan_duration=5.0
    )
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        _inject(scanner, "11:22:33:44:55:66")
        entries = sched._needs["11:22:33:44:55:66"]
        request = next(iter(entries))
        entries[request] = loop.time() - 1.0
        sched._async_tick()
        await _drain()
        assert scanner.active_window_calls == [5.0]
        assert entries[request] > loop.time()
    finally:
        cancel()
        register_cancel()


@pytest.mark.asyncio
async def test_tick_coalesces_overlapping_requests() -> None:
    """Two requests for the same address coalesce into one max-duration window."""
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    address = "11:22:33:44:55:66"
    cancel1 = manager.async_register_active_scan(
        address, scan_interval=120.0, scan_duration=3.0
    )
    cancel2 = manager.async_register_active_scan(
        address, scan_interval=120.0, scan_duration=10.0
    )
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        _inject(scanner, address)
        entries = sched._needs[address]
        for req in list(entries):
            entries[req] = loop.time() - 1.0
        sched._async_tick()
        await _drain()
        assert scanner.active_window_calls == [10.0]
    finally:
        cancel1()
        cancel2()
        register_cancel()


@pytest.mark.asyncio
async def test_multiple_requests_same_address_track_independent_intervals() -> None:
    """Two registrations for the same address fire on their own cadences."""
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    address = "11:22:33:44:55:66"
    cancel_fast = manager.async_register_active_scan(
        address, scan_interval=60.0, scan_duration=2.0
    )
    cancel_slow = manager.async_register_active_scan(
        address, scan_interval=300.0, scan_duration=4.0
    )
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        _inject(scanner, address)
        entries = sched._needs[address]
        assert len(entries) == 2
        fast, slow = sorted(entries, key=lambda r: r.scan_interval)
        entries[fast] = loop.time() - 1.0
        entries[slow] = loop.time() + 200.0
        sched._async_tick()
        await _drain()
        assert scanner.active_window_calls == [2.0]
        assert entries[fast] > loop.time()
        assert entries[slow] > loop.time() + 100
        sched._scanner_windows.clear()
        entries[fast] = loop.time() - 1.0
        entries[slow] = loop.time() - 1.0
        sched._async_tick()
        await _drain()
        assert scanner.active_window_calls == [2.0, 4.0]
    finally:
        cancel_fast()
        cancel_slow()
        register_cancel()


@pytest.mark.asyncio
async def test_tick_skips_non_auto_scanner() -> None:
    """ACTIVE / PASSIVE scanners are not asked to flip; due times advance."""
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    address = "11:22:33:44:55:66"
    cancel = manager.async_register_active_scan(
        address, scan_interval=120.0, scan_duration=3.0
    )
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.ACTIVE)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        _inject(scanner, address)
        entries = sched._needs.get(address, {})
        for req in list(entries):
            entries[req] = loop.time() - 1.0
        sched._async_tick()
        await _drain()
        assert scanner.active_window_calls == []
    finally:
        cancel()
        register_cancel()


@pytest.mark.asyncio
async def test_global_sweep_runs_on_auto_scanner() -> None:
    """The 4h sweep fires async_request_active_window with SWEEP_DURATION."""
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        sched._sweep_last_completed["AA:BB:CC:DD:EE:00"] = (
            loop.time() - AUTO_REDISCOVERY_INTERVAL - 1.0
        )
        sched._async_tick()
        await _drain()
        assert scanner.active_window_calls == [AUTO_REDISCOVERY_SWEEP_DURATION]
        assert sched._sweep_in_flight is None
    finally:
        register_cancel()


@pytest.mark.asyncio
async def test_global_sweep_one_scanner_at_a_time() -> None:
    """While one scanner sweeps, no other scanner is asked to sweep."""
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    blocking = asyncio.Event()
    s1 = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    s1._block_event = blocking
    s2 = _RecordingAutoScanner("AA:BB:CC:DD:EE:11", BluetoothScanningMode.AUTO)
    c1 = manager.async_register_scanner(s1)
    c2 = manager.async_register_scanner(s2)
    try:
        now = loop.time()
        sched._sweep_last_completed["AA:BB:CC:DD:EE:00"] = (
            now - AUTO_REDISCOVERY_INTERVAL - 10
        )
        sched._sweep_last_completed["AA:BB:CC:DD:EE:11"] = (
            now - AUTO_REDISCOVERY_INTERVAL - 5
        )
        sched._async_tick()
        await _drain()
        assert sched._sweep_in_flight == "AA:BB:CC:DD:EE:00"
        sched._async_tick()
        await _drain()
        assert s2.active_window_calls == []
        blocking.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert sched._sweep_in_flight is None
    finally:
        blocking.set()
        c1()
        c2()


@pytest.mark.asyncio
async def test_remove_request_clears_tracking() -> None:
    """Cancelling a registration removes its per-(address, request) entries."""
    manager = get_manager()
    sched = manager._auto_scheduler
    address = "11:22:33:44:55:66"
    cancel = manager.async_register_active_scan(address, scan_interval=60.0)
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        _inject(scanner, address)
        assert address in sched._needs
        cancel()
        assert address not in sched._needs
        assert sched._requests_by_address == {}
    finally:
        register_cancel()


@pytest.mark.asyncio
async def test_busy_scanner_defers_due_callbacks_not_busy_loops() -> None:
    """A scanner mid-window pushes due requests past the window end."""
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    address = "11:22:33:44:55:66"
    cancel = manager.async_register_active_scan(
        address, scan_interval=60.0, scan_duration=3.0
    )
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        _inject(scanner, address)
        entries = sched._needs[address]
        request = next(iter(entries))
        entries[request] = loop.time() - 1.0
        busy_end = loop.time() + 5.0
        sched._scanner_windows[scanner.source] = busy_end
        sched._async_tick()
        await _drain()
        assert scanner.active_window_calls == []
        assert entries[request] >= busy_end
    finally:
        cancel()
        register_cancel()


@pytest.mark.asyncio
async def test_failed_request_clears_busy_marker() -> None:
    """A False return from async_request_active_window frees the scanner."""
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    address = "11:22:33:44:55:66"
    cancel = manager.async_register_active_scan(
        address, scan_interval=60.0, scan_duration=3.0
    )
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    scanner._return_value = False
    register_cancel = manager.async_register_scanner(scanner)
    try:
        _inject(scanner, address)
        entries = sched._needs[address]
        for req in list(entries):
            entries[req] = loop.time() - 1.0
        sched._async_tick()
        await _drain()
        assert scanner.active_window_calls == [3.0]
        assert scanner.source not in sched._scanner_windows
    finally:
        cancel()
        register_cancel()


@pytest.mark.asyncio
async def test_failed_sweep_advances_sweep_last_completed() -> None:
    """A False return on a sweep updates _sweep_last_completed so we don't busy-loop."""
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    scanner._return_value = False
    register_cancel = manager.async_register_scanner(scanner)
    try:
        sched._sweep_last_completed["AA:BB:CC:DD:EE:00"] = (
            loop.time() - AUTO_REDISCOVERY_INTERVAL - 1.0
        )
        before = sched._sweep_last_completed["AA:BB:CC:DD:EE:00"]
        sched._async_tick()
        await _drain()
        assert scanner.active_window_calls == [AUTO_REDISCOVERY_SWEEP_DURATION]
        # _sweep_last_completed was advanced even though the window failed,
        # so the next sweep is one full interval out instead of immediate.
        assert sched._sweep_last_completed["AA:BB:CC:DD:EE:00"] > before
        assert sched._sweep_in_flight is None
    finally:
        register_cancel()


@pytest.mark.asyncio
async def test_stop_cancels_pending_window_tasks() -> None:
    """Scheduler.stop cancels in-flight active-window tasks."""
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    blocking = asyncio.Event()
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    scanner._block_event = blocking
    register_cancel = manager.async_register_scanner(scanner)
    try:
        sched._sweep_last_completed["AA:BB:CC:DD:EE:00"] = (
            loop.time() - AUTO_REDISCOVERY_INTERVAL - 1.0
        )
        sched._async_tick()
        await _drain()
        assert len(sched._pending_tasks) == 1
        pending = next(iter(sched._pending_tasks))
        sched.stop()
        await asyncio.sleep(0)
        assert pending.cancelled() or pending.done()
        assert sched._pending_tasks == set()
        assert sched._scanner_windows == {}
        assert sched._sweep_in_flight is None
    finally:
        blocking.set()
        register_cancel()


@pytest.mark.asyncio
async def test_dispatch_drops_tracking_for_unseen_address() -> None:
    """A due address with no history entry is pruned, not retried."""
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    cancel = manager.async_register_active_scan("aa:bb:cc:dd:ee:ff", scan_interval=60.0)
    try:
        request = next(iter(sched._requests_by_address["aa:bb:cc:dd:ee:ff"]))
        sched._needs["aa:bb:cc:dd:ee:ff"] = {request: loop.time() - 1.0}
        sched._async_tick()
        assert "aa:bb:cc:dd:ee:ff" not in sched._needs
    finally:
        cancel()


@pytest.mark.asyncio
async def test_first_sweep_is_delayed_after_scanner_registers() -> None:
    """A newly registered AUTO scanner's first sweep is AUTO_INITIAL_SWEEP_DELAY out."""
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        last = sched._sweep_last_completed["AA:BB:CC:DD:EE:00"]
        first_sweep_at = last + AUTO_REDISCOVERY_INTERVAL
        now = loop.time()
        # First sweep should land roughly AUTO_INITIAL_SWEEP_DELAY from now,
        # not AUTO_REDISCOVERY_INTERVAL from now.
        assert (
            AUTO_INITIAL_SWEEP_DELAY - 1.0
            <= first_sweep_at - now
            <= AUTO_INITIAL_SWEEP_DELAY + 1.0
        )
        # Force the tick at the scheduled first sweep time and confirm
        # the sweep would fire.
        sched._sweep_last_completed["AA:BB:CC:DD:EE:00"] = (
            now - AUTO_REDISCOVERY_INTERVAL - 1.0
        )
        sched._async_tick()
        await _drain()
        assert scanner.active_window_calls == [AUTO_REDISCOVERY_SWEEP_DURATION]
    finally:
        register_cancel()


@pytest.mark.asyncio
async def test_remove_scanner_clears_sweep_state() -> None:
    """Unregistering a scanner drops its sweep / window state."""
    manager = get_manager()
    sched = manager._auto_scheduler
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    cancel = manager.async_register_scanner(scanner)
    assert "AA:BB:CC:DD:EE:00" in sched._sweep_last_completed
    cancel()
    assert "AA:BB:CC:DD:EE:00" not in sched._sweep_last_completed


@pytest.mark.asyncio
async def test_remove_scanner_clears_sweep_in_flight() -> None:
    """Unregistering a scanner mid-sweep resets _sweep_in_flight."""
    manager = get_manager()
    sched = manager._auto_scheduler
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    cancel = manager.async_register_scanner(scanner)
    sched._sweep_in_flight = scanner.source
    cancel()
    assert sched._sweep_in_flight is None


@pytest.mark.asyncio
async def test_add_scanner_before_start_stores_placeholder() -> None:
    """A scanner registered before start() leaves a placeholder until start runs."""
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = sched._loop
    assert loop is not None
    sched._loop = None
    try:
        scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
        sched.add_scanner(scanner)
        assert sched._sweep_last_completed["AA:BB:CC:DD:EE:00"] == 0.0
        manager._sources[scanner.source] = scanner
        sched.start(loop)
        assert sched._sweep_last_completed["AA:BB:CC:DD:EE:00"] > 0.0
    finally:
        manager._sources.pop("AA:BB:CC:DD:EE:00", None)
        sched._sweep_last_completed.pop("AA:BB:CC:DD:EE:00", None)


@pytest.mark.asyncio
async def test_stop_is_safe_when_already_idle() -> None:
    """Calling stop() twice in a row is fully idempotent."""
    manager = get_manager()
    sched = manager._auto_scheduler
    sched.stop()
    sched.stop()
    assert sched._tick_handle is None
    assert sched._pending_tasks == set()
    assert sched._scanner_windows == {}
    assert sched._sweep_in_flight is None


@pytest.mark.asyncio
async def test_duration_clamped_to_bounds() -> None:
    """_coalesce_duration clamps the requested duration to the configured range."""
    sched = get_manager()._auto_scheduler

    def _req(duration: float | None) -> ActiveScanRequest:
        return ActiveScanRequest("AA", 60.0, duration)

    assert sched._coalesce_duration([_req(0.01)]) == AUTO_WINDOW_MIN_DURATION
    assert sched._coalesce_duration([_req(1000.0)]) == AUTO_WINDOW_MAX_DURATION
    assert sched._coalesce_duration([_req(7.5)]) == 7.5
    assert sched._coalesce_duration([_req(0.01), _req(7.5)]) == 7.5
    assert (
        sched._coalesce_duration([_req(7.5), _req(1000.0)]) == AUTO_WINDOW_MAX_DURATION
    )
    assert sched._coalesce_duration([_req(None)]) == AUTO_WINDOW_MIN_DURATION


@pytest.mark.asyncio
async def test_on_advertisement_early_returns_with_no_requests() -> None:
    """Hot path is a no-op when no active-scan request is registered."""
    manager = get_manager()
    sched = manager._auto_scheduler
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        _inject(scanner, "11:22:33:44:55:66")
        assert sched._needs == {}
        assert sched._requests_by_address == {}
    finally:
        register_cancel()
