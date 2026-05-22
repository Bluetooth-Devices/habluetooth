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
    """Yield several times so worker tasks can process."""
    for _ in range(4):
        await asyncio.sleep(0)


async def _run_worker_tick(scheduler: object, source: str) -> None:
    """Drive one worker through a single tick for deterministic testing."""
    worker = scheduler._workers[source]  # type: ignore[attr-defined]
    await worker._tick()


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
async def test_worker_tick_fires_active_window() -> None:
    """A due tracker entry causes the owning scanner's worker to fire a window."""
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
        await _run_worker_tick(sched, scanner.source)
        assert scanner.active_window_calls == [5.0]
        assert entries[request] > loop.time()
    finally:
        cancel()
        register_cancel()


@pytest.mark.asyncio
async def test_worker_tick_coalesces_overlapping_requests() -> None:
    """Multiple requests for the same address coalesce on max scan_duration."""
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
        await _run_worker_tick(sched, scanner.source)
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
        await _run_worker_tick(sched, scanner.source)
        assert scanner.active_window_calls == [2.0]
        assert entries[fast] > loop.time()
        assert entries[slow] > loop.time() + 100
        entries[fast] = loop.time() - 1.0
        entries[slow] = loop.time() - 1.0
        await _run_worker_tick(sched, scanner.source)
        assert scanner.active_window_calls == [2.0, 4.0]
    finally:
        cancel_fast()
        cancel_slow()
        register_cancel()


@pytest.mark.asyncio
async def test_no_worker_for_non_auto_scanner() -> None:
    """ACTIVE / PASSIVE scanners don't get a worker; their windows are never fired."""
    manager = get_manager()
    sched = manager._auto_scheduler
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.ACTIVE)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        assert scanner.source not in sched._workers
    finally:
        register_cancel()


@pytest.mark.asyncio
async def test_global_sweep_runs_on_auto_scanner() -> None:
    """The sweep fires async_request_active_window with SWEEP_DURATION."""
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        worker = sched._workers[scanner.source]
        worker._sweep_last_completed = loop.time() - AUTO_REDISCOVERY_INTERVAL - 1.0
        await _run_worker_tick(sched, scanner.source)
        assert scanner.active_window_calls == [AUTO_REDISCOVERY_SWEEP_DURATION]
        assert worker._sweep_last_completed > loop.time() - 1.0
    finally:
        register_cancel()


@pytest.mark.asyncio
async def test_global_sweep_one_scanner_at_a_time() -> None:
    """Two scanners both due for sweep do not sweep concurrently."""
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
        w1 = sched._workers[s1.source]
        w2 = sched._workers[s2.source]
        w1._sweep_last_completed = loop.time() - AUTO_REDISCOVERY_INTERVAL - 10
        w2._sweep_last_completed = loop.time() - AUTO_REDISCOVERY_INTERVAL - 5
        # Kick worker 1 into its sweep (it'll block on the asyncio.Event).
        t1 = asyncio.create_task(w1._tick())
        await _drain()
        # While w1's sweep is blocked, w2 attempts its sweep too. It must
        # wait on the shared _sweep_lock and not fire concurrently.
        t2 = asyncio.create_task(w2._tick())
        await _drain()
        assert s1.active_window_calls == [AUTO_REDISCOVERY_SWEEP_DURATION]
        assert s2.active_window_calls == []
        blocking.set()
        await t1
        await t2
        assert s2.active_window_calls == [AUTO_REDISCOVERY_SWEEP_DURATION]
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
async def test_failed_sweep_advances_sweep_last_completed() -> None:
    """A False return on a sweep advances the worker's sweep clock."""
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    scanner._return_value = False
    register_cancel = manager.async_register_scanner(scanner)
    try:
        worker = sched._workers[scanner.source]
        worker._sweep_last_completed = loop.time() - AUTO_REDISCOVERY_INTERVAL - 1.0
        before = worker._sweep_last_completed
        await _run_worker_tick(sched, scanner.source)
        assert scanner.active_window_calls == [AUTO_REDISCOVERY_SWEEP_DURATION]
        # Even on False, the worker's sweep clock advanced so the next
        # sweep is one full interval out instead of immediate.
        assert worker._sweep_last_completed > before
    finally:
        register_cancel()


@pytest.mark.asyncio
async def test_stop_cancels_worker_tasks() -> None:
    """Scheduler.stop cancels every worker task."""
    manager = get_manager()
    sched = manager._auto_scheduler
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        worker = sched._workers[scanner.source]
        task = worker._task
        assert task is not None
        sched.stop()
        await asyncio.sleep(0)
        assert task.cancelled() or task.done()
        assert sched._workers == {}
    finally:
        register_cancel()


@pytest.mark.asyncio
async def test_dispatch_drops_tracking_for_unseen_address() -> None:
    """An address with no history entry is pruned on the next worker tick."""
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    cancel = manager.async_register_active_scan("aa:bb:cc:dd:ee:ff", scan_interval=60.0)
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        request = next(iter(sched._requests_by_address["aa:bb:cc:dd:ee:ff"]))
        sched._needs["aa:bb:cc:dd:ee:ff"] = {request: loop.time() - 1.0}
        await _run_worker_tick(sched, scanner.source)
        assert "aa:bb:cc:dd:ee:ff" not in sched._needs
    finally:
        cancel()
        register_cancel()


@pytest.mark.asyncio
async def test_first_sweep_is_delayed_after_scanner_registers() -> None:
    """A newly registered AUTO scanner's first sweep is AUTO_INITIAL_SWEEP_DELAY out."""
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        worker = sched._workers[scanner.source]
        first_sweep_at = worker._sweep_last_completed + AUTO_REDISCOVERY_INTERVAL
        now = loop.time()
        assert (
            AUTO_INITIAL_SWEEP_DELAY - 1.0
            <= first_sweep_at - now
            <= AUTO_INITIAL_SWEEP_DELAY + 1.0
        )
    finally:
        register_cancel()


@pytest.mark.asyncio
async def test_remove_scanner_stops_its_worker() -> None:
    """Unregistering a scanner cancels and drops its worker."""
    manager = get_manager()
    sched = manager._auto_scheduler
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    cancel = manager.async_register_scanner(scanner)
    assert scanner.source in sched._workers
    worker = sched._workers[scanner.source]
    task = worker._task
    cancel()
    await asyncio.sleep(0)
    assert scanner.source not in sched._workers
    assert task is not None
    assert task.cancelled() or task.done()


@pytest.mark.asyncio
async def test_add_scanner_before_start_defers_worker() -> None:
    """A scanner registered before start() gets its worker on start()."""
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = sched._loop
    assert loop is not None
    sched._loop = None
    try:
        scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
        sched.add_scanner(scanner)
        assert scanner.source not in sched._workers
        manager._sources[scanner.source] = scanner
        sched.start(loop)
        assert scanner.source in sched._workers
        sched._workers[scanner.source].stop()
    finally:
        manager._sources.pop("AA:BB:CC:DD:EE:00", None)


@pytest.mark.asyncio
async def test_stop_is_safe_when_already_idle() -> None:
    """Calling stop() twice in a row is fully idempotent."""
    manager = get_manager()
    sched = manager._auto_scheduler
    sched.stop()
    sched.stop()
    assert sched._workers == {}


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


@pytest.mark.asyncio
async def test_on_advertisement_wakes_owning_worker() -> None:
    """Adding a tracking entry wakes the worker so it picks the new event up."""
    manager = get_manager()
    sched = manager._auto_scheduler
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    cancel = manager.async_register_active_scan(
        "11:22:33:44:55:66", scan_interval=120.0
    )
    try:
        worker = sched._workers[scanner.source]
        worker._wake.clear()
        _inject(scanner, "11:22:33:44:55:66")
        assert worker._wake.is_set()
    finally:
        cancel()
        register_cancel()


@pytest.mark.asyncio
async def test_register_active_scan_validates_inputs() -> None:
    """Invalid scan_interval / scan_duration raise ValueError."""
    manager = get_manager()
    with pytest.raises(ValueError, match="scan_interval must be > 0"):
        manager.async_register_active_scan("AA:BB:CC:DD:EE:00", scan_interval=0)
    with pytest.raises(ValueError, match="scan_interval must be > 0"):
        manager.async_register_active_scan("AA:BB:CC:DD:EE:00", scan_interval=-1)
    with pytest.raises(ValueError, match="scan_duration must be None or >= 0"):
        manager.async_register_active_scan(
            "AA:BB:CC:DD:EE:00", scan_interval=60.0, scan_duration=-0.5
        )


@pytest.mark.asyncio
async def test_run_window_swallows_scanner_exception() -> None:
    """An exception from async_request_active_window is logged, not re-raised."""
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()

    class _FailingScanner(_RecordingAutoScanner):
        async def async_request_active_window(self, duration: float) -> bool:
            raise RuntimeError("boom")

    scanner = _FailingScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        worker = sched._workers[scanner.source]
        worker._sweep_last_completed = loop.time() - AUTO_REDISCOVERY_INTERVAL - 1.0
        await worker._tick()
        # The exception was swallowed; sweep state still advanced.
        assert worker._sweep_last_completed > loop.time() - 1.0
    finally:
        register_cancel()


@pytest.mark.asyncio
async def test_dispatch_does_not_resurrect_cancelled_request() -> None:
    """A request cancelled while the window awaits is not re-added to entries."""
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    address = "11:22:33:44:55:66"
    cancel = manager.async_register_active_scan(
        address, scan_interval=60.0, scan_duration=3.0
    )

    gate = asyncio.Event()

    class _CancelDuringWindow(_RecordingAutoScanner):
        async def async_request_active_window(self, duration: float) -> bool:
            # Mid-window: caller cancels the registration.
            cancel()
            gate.set()
            return True

    scanner = _CancelDuringWindow("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        _inject(scanner, address)
        entries = sched._needs[address]
        request = next(iter(entries))
        entries[request] = loop.time() - 1.0
        await sched._workers[scanner.source]._tick()
        await gate.wait()
        # remove_request emptied the bucket; the tick must not have
        # re-added the cancelled request.
        assert address not in sched._needs
    finally:
        register_cancel()


@pytest.mark.asyncio
async def test_dispatch_skips_address_owned_by_other_scanner() -> None:
    """An address whose owner is a different scanner is left alone."""
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    address = "11:22:33:44:55:66"
    cancel = manager.async_register_active_scan(address, scan_interval=60.0)
    owner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    other = _RecordingAutoScanner("AA:BB:CC:DD:EE:11", BluetoothScanningMode.AUTO)
    c1 = manager.async_register_scanner(owner)
    c2 = manager.async_register_scanner(other)
    try:
        _inject(owner, address)
        entries = sched._needs[address]
        for req in list(entries):
            entries[req] = loop.time() - 1.0
        # The "other" scanner runs its tick. The address is owned by
        # owner, so other should not fire its window.
        await sched._workers[other.source]._tick()
        assert other.active_window_calls == []
    finally:
        cancel()
        c1()
        c2()


@pytest.mark.asyncio
async def test_next_event_at_returns_current_window_end() -> None:
    """While a window is in flight, next event is its end time."""
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        worker = sched._workers[scanner.source]
        worker._window_end = loop.time() + 42.0
        assert worker._next_event_at(loop.time()) == worker._window_end
    finally:
        register_cancel()
