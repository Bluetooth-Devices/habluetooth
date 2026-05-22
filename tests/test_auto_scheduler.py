"""Tests for the auto-mode active-window scheduler."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any

import pytest
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from habluetooth import (
    BaseHaScanner,
    BluetoothScanningMode,
    get_manager,
)
from habluetooth.const import (
    AUTO_REDISCOVERY_INTERVAL,
    AUTO_REDISCOVERY_SWEEP_DURATION,
    AUTO_WINDOW_MAX_DURATION,
    AUTO_WINDOW_MIN_DURATION,
)
from habluetooth.manager import BleakCallback

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


async def _drain(loop: asyncio.AbstractEventLoop) -> None:
    """Yield once so scheduled tasks run."""
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_register_active_without_interval_warns() -> None:
    """ACTIVE registration without scan_interval emits DeprecationWarning."""
    manager = get_manager()

    def _cb(_device: Any, _adv: Any) -> None: ...

    with pytest.warns(DeprecationWarning, match="scan_interval"):
        cancel = manager.async_register_bleak_callback(
            _cb, {}, scanning_mode=BluetoothScanningMode.ACTIVE
        )
    cancel()


@pytest.mark.asyncio
async def test_register_active_with_interval_does_not_warn(
    recwarn: pytest.WarningsRecorder,
) -> None:
    """ACTIVE with scan_interval should not emit the deprecation warning."""
    manager = get_manager()

    def _cb(_device: Any, _adv: Any) -> None: ...

    cancel = manager.async_register_bleak_callback(
        _cb,
        {},
        scanning_mode=BluetoothScanningMode.ACTIVE,
        scan_interval=300.0,
        scan_duration=5.0,
    )
    cancel()
    assert not any(
        issubclass(w.category, DeprecationWarning) and "scan_interval" in str(w.message)
        for w in recwarn.list
    )


@pytest.mark.asyncio
async def test_passive_or_auto_no_warn(recwarn: pytest.WarningsRecorder) -> None:
    """PASSIVE / AUTO / unset registrations never emit the deprecation."""
    manager = get_manager()

    def _cb(_device: Any, _adv: Any) -> None: ...

    for mode in (None, BluetoothScanningMode.PASSIVE, BluetoothScanningMode.AUTO):
        cancel = manager.async_register_bleak_callback(_cb, {}, scanning_mode=mode)
        cancel()
    assert not any(
        issubclass(w.category, DeprecationWarning) and "scan_interval" in str(w.message)
        for w in recwarn.list
    )


@pytest.mark.asyncio
async def test_advertisement_starts_tracking() -> None:
    """on_advertisement should add a per-(address, callback) tracker entry."""
    manager = get_manager()
    sched = manager._auto_scheduler

    def _cb(_device: Any, _adv: Any) -> None: ...

    cancel = manager.async_register_bleak_callback(
        _cb,
        {},
        scanning_mode=BluetoothScanningMode.AUTO,
        scan_interval=120.0,
        scan_duration=3.0,
    )

    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        device = generate_ble_device("11:22:33:44:55:66", "inkbird")
        adv = generate_advertisement_data(local_name="inkbird")
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
        assert "11:22:33:44:55:66" in sched._needs
        assert len(sched._needs["11:22:33:44:55:66"]) == 1
    finally:
        cancel()
        register_cancel()
    assert sched._needs == {}


@pytest.mark.asyncio
async def test_tick_requests_active_window_on_auto_scanner() -> None:
    """When a tracker entry is due, the tick should call the scanner."""
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()

    def _cb(_device: Any, _adv: Any) -> None: ...

    cancel = manager.async_register_bleak_callback(
        _cb,
        {},
        scanning_mode=BluetoothScanningMode.AUTO,
        scan_interval=120.0,
        scan_duration=5.0,
    )
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        device = generate_ble_device("11:22:33:44:55:66", "inkbird")
        adv = generate_advertisement_data(local_name="inkbird")
        scanner._async_on_advertisement(
            device.address,
            adv.rssi,
            device.name or "",
            adv.service_uuids,
            adv.service_data,
            adv.manufacturer_data,
            adv.tx_power,
            {},
            loop.time(),
        )
        # Force the entry due.
        callbacks = sched._needs["11:22:33:44:55:66"]
        bleak_callback = next(iter(callbacks))
        callbacks[bleak_callback] = loop.time() - 1.0
        sched._async_tick()
        await _drain(loop)
        assert scanner.active_window_calls == [5.0]
        # next_due was advanced.
        assert callbacks[bleak_callback] > loop.time()
    finally:
        cancel()
        register_cancel()


@pytest.mark.asyncio
async def test_tick_coalesces_overlapping_callbacks() -> None:
    """Two callbacks for the same address coalesce into one window with max duration."""
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()

    def _cb1(_device: Any, _adv: Any) -> None: ...

    def _cb2(_device: Any, _adv: Any) -> None: ...

    cancel1 = manager.async_register_bleak_callback(
        _cb1,
        {},
        scanning_mode=BluetoothScanningMode.AUTO,
        scan_interval=120.0,
        scan_duration=3.0,
    )
    cancel2 = manager.async_register_bleak_callback(
        _cb2,
        {},
        scanning_mode=BluetoothScanningMode.AUTO,
        scan_interval=120.0,
        scan_duration=10.0,
    )
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        device = generate_ble_device("11:22:33:44:55:66", "x")
        adv = generate_advertisement_data(local_name="x")
        scanner._async_on_advertisement(
            device.address,
            adv.rssi,
            device.name or "",
            adv.service_uuids,
            adv.service_data,
            adv.manufacturer_data,
            adv.tx_power,
            {},
            loop.time(),
        )
        callbacks = sched._needs["11:22:33:44:55:66"]
        for cb in list(callbacks):
            callbacks[cb] = loop.time() - 1.0
        sched._async_tick()
        await _drain(loop)
        assert scanner.active_window_calls == [10.0]
    finally:
        cancel1()
        cancel2()
        register_cancel()


@pytest.mark.asyncio
async def test_tick_skips_non_auto_scanner() -> None:
    """An ACTIVE/PASSIVE scanner is not asked to run extra windows."""
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()

    def _cb(_device: Any, _adv: Any) -> None: ...

    cancel = manager.async_register_bleak_callback(
        _cb,
        {},
        scanning_mode=BluetoothScanningMode.AUTO,
        scan_interval=120.0,
        scan_duration=3.0,
    )
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.ACTIVE)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        device = generate_ble_device("11:22:33:44:55:66", "x")
        adv = generate_advertisement_data(local_name="x")
        scanner._async_on_advertisement(
            device.address,
            adv.rssi,
            device.name or "",
            adv.service_uuids,
            adv.service_data,
            adv.manufacturer_data,
            adv.tx_power,
            {},
            loop.time(),
        )
        callbacks = sched._needs.get("11:22:33:44:55:66", {})
        for cb in list(callbacks):
            callbacks[cb] = loop.time() - 1.0
        sched._async_tick()
        await _drain(loop)
        assert scanner.active_window_calls == []
    finally:
        cancel()
        register_cancel()


@pytest.mark.asyncio
async def test_global_sweep_runs_on_auto_scanner() -> None:
    """The 4 h sweep fires async_request_active_window with SWEEP_DURATION."""
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()

    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        # Force the scanner's "last sweep" to be older than the interval.
        sched._sweep_last_completed["AA:BB:CC:DD:EE:00"] = (
            loop.time() - AUTO_REDISCOVERY_INTERVAL - 1.0
        )
        sched._async_tick()
        await _drain(loop)
        assert scanner.active_window_calls == [AUTO_REDISCOVERY_SWEEP_DURATION]
        assert sched._sweep_in_flight is None  # cleared after window completes
    finally:
        register_cancel()


@pytest.mark.asyncio
async def test_global_sweep_one_scanner_at_a_time() -> None:
    """While one scanner sweeps, no other scanner is asked to sweep."""
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()

    # Block the first scanner's active-window task so the sweep stays in flight.
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
        await _drain(loop)
        assert sched._sweep_in_flight == "AA:BB:CC:DD:EE:00"
        # A second tick must NOT start s2's sweep while s1's is in flight.
        sched._async_tick()
        await _drain(loop)
        assert s2.active_window_calls == []
        blocking.set()
        # Drain the now-completed sweep.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert sched._sweep_in_flight is None
    finally:
        c1()
        c2()


@pytest.mark.asyncio
async def test_remove_callback_clears_tracking() -> None:
    """Removing a registered callback prunes its per-(address, cb) entries."""
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()

    def _cb(_device: Any, _adv: Any) -> None: ...

    cancel = manager.async_register_bleak_callback(
        _cb,
        {},
        scanning_mode=BluetoothScanningMode.AUTO,
        scan_interval=60.0,
    )
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        device = generate_ble_device("11:22:33:44:55:66", "x")
        adv = generate_advertisement_data(local_name="x")
        scanner._async_on_advertisement(
            device.address,
            adv.rssi,
            device.name or "",
            adv.service_uuids,
            adv.service_data,
            adv.manufacturer_data,
            adv.tx_power,
            {},
            loop.time(),
        )
        assert "11:22:33:44:55:66" in sched._needs
        cancel()
        assert "11:22:33:44:55:66" not in sched._needs
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
async def test_duration_clamped_to_bounds() -> None:
    """_coalesce_duration clamps the requested duration to the configured range."""
    manager = get_manager()
    sched = manager._auto_scheduler

    def _cb(_device: Any, _adv: Any) -> None: ...

    too_small = BleakCallback(_cb, {}, scan_duration=0.01)
    too_big = BleakCallback(_cb, {}, scan_duration=1000.0)
    in_range = BleakCallback(_cb, {}, scan_duration=7.5)

    assert sched._coalesce_duration([too_small]) == AUTO_WINDOW_MIN_DURATION
    assert sched._coalesce_duration([too_big]) == AUTO_WINDOW_MAX_DURATION
    assert sched._coalesce_duration([in_range]) == 7.5
    # max() then clamp; the largest wins.
    assert sched._coalesce_duration([too_small, in_range]) == 7.5
    assert sched._coalesce_duration([in_range, too_big]) == AUTO_WINDOW_MAX_DURATION


@pytest.mark.asyncio
async def test_on_advertisement_early_returns_with_no_interval_callbacks() -> None:
    """Hot path is a no-op when no callback declared a scan_interval."""
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()

    def _cb(_device: Any, _adv: Any) -> None: ...

    # A regular bleak callback (no scan_interval) should NOT populate _needs.
    cancel = manager.async_register_bleak_callback(_cb, {})
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        device = generate_ble_device("11:22:33:44:55:66", "x")
        adv = generate_advertisement_data(local_name="x")
        scanner._async_on_advertisement(
            device.address,
            adv.rssi,
            device.name or "",
            adv.service_uuids,
            adv.service_data,
            adv.manufacturer_data,
            adv.tx_power,
            {},
            loop.time(),
        )
        assert sched._needs == {}
        assert sched._interval_callbacks == set()
    finally:
        cancel()
        register_cancel()


@pytest.mark.asyncio
async def test_busy_scanner_defers_due_callbacks_not_busy_loops() -> None:
    """If a scanner is mid-window, due callbacks are pushed past the window."""
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()

    def _cb(_device: Any, _adv: Any) -> None: ...

    cancel = manager.async_register_bleak_callback(
        _cb,
        {},
        scanning_mode=BluetoothScanningMode.AUTO,
        scan_interval=60.0,
        scan_duration=3.0,
    )
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        device = generate_ble_device("11:22:33:44:55:66", "x")
        adv = generate_advertisement_data(local_name="x")
        scanner._async_on_advertisement(
            device.address,
            adv.rssi,
            device.name or "",
            adv.service_uuids,
            adv.service_data,
            adv.manufacturer_data,
            adv.tx_power,
            {},
            loop.time(),
        )
        callbacks = sched._needs["11:22:33:44:55:66"]
        bleak_callback = next(iter(callbacks))
        # Make the callback due and the scanner busy until 5s from now.
        callbacks[bleak_callback] = loop.time() - 1.0
        busy_end = loop.time() + 5.0
        sched._scanner_windows[scanner.source] = busy_end
        sched._async_tick()
        await _drain(loop)
        # No window request fired (scanner busy).
        assert scanner.active_window_calls == []
        # The callback's due time was deferred past the busy window.
        assert callbacks[bleak_callback] >= busy_end
    finally:
        cancel()
        register_cancel()


@pytest.mark.asyncio
async def test_failed_request_clears_busy_marker() -> None:
    """A False return from async_request_active_window frees the scanner immediately."""
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()

    def _cb(_device: Any, _adv: Any) -> None: ...

    cancel = manager.async_register_bleak_callback(
        _cb,
        {},
        scanning_mode=BluetoothScanningMode.AUTO,
        scan_interval=60.0,
        scan_duration=3.0,
    )
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    scanner._return_value = False
    register_cancel = manager.async_register_scanner(scanner)
    try:
        device = generate_ble_device("11:22:33:44:55:66", "x")
        adv = generate_advertisement_data(local_name="x")
        scanner._async_on_advertisement(
            device.address,
            adv.rssi,
            device.name or "",
            adv.service_uuids,
            adv.service_data,
            adv.manufacturer_data,
            adv.tx_power,
            {},
            loop.time(),
        )
        callbacks = sched._needs["11:22:33:44:55:66"]
        for cb in list(callbacks):
            callbacks[cb] = loop.time() - 1.0
        sched._async_tick()
        await _drain(loop)
        # Scanner was asked; it returned False; busy marker was cleared.
        assert scanner.active_window_calls == [3.0]
        assert scanner.source not in sched._scanner_windows
    finally:
        cancel()
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
        await _drain(loop)
        assert len(sched._pending_tasks) == 1
        pending = next(iter(sched._pending_tasks))
        sched.stop()
        # Yield so the cancelled task can settle.
        await asyncio.sleep(0)
        assert pending.cancelled() or pending.done()
        assert sched._pending_tasks == set()
        assert sched._scanner_windows == {}
        assert sched._sweep_in_flight is None
    finally:
        # Let the blocked task exit cleanly even after cancellation so the
        # event loop has no dangling waiter at fixture teardown.
        blocking.set()
        register_cancel()
