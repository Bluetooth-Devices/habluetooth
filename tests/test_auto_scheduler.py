"""Tests for the auto-mode active-window scheduler."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterable

import pytest
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from habluetooth import (
    BaseHaScanner,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
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
        # The registered address has tracking from add_request; the
        # unrelated advertisement must not create its own entry.
        _inject(scanner, "AA:AA:AA:AA:AA:AA")
        assert "AA:AA:AA:AA:AA:AA" not in sched._needs
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
async def test_first_sweeps_stagger_across_scanners() -> None:
    """Concurrently-registered scanners get offset first-sweep times."""
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    s1 = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    s2 = _RecordingAutoScanner("AA:BB:CC:DD:EE:11", BluetoothScanningMode.AUTO)
    s3 = _RecordingAutoScanner("AA:BB:CC:DD:EE:22", BluetoothScanningMode.AUTO)
    c1 = manager.async_register_scanner(s1)
    c2 = manager.async_register_scanner(s2)
    c3 = manager.async_register_scanner(s3)
    try:
        now = loop.time()
        sweep_1 = (
            sched._workers[s1.source]._sweep_last_completed + AUTO_REDISCOVERY_INTERVAL
        )
        sweep_2 = (
            sched._workers[s2.source]._sweep_last_completed + AUTO_REDISCOVERY_INTERVAL
        )
        sweep_3 = (
            sched._workers[s3.source]._sweep_last_completed + AUTO_REDISCOVERY_INTERVAL
        )
        # Each subsequent worker's first sweep is one sweep-duration
        # later than the previous one's (slack for loop.time() advancing
        # between spawn calls).
        assert sweep_2 - sweep_1 == pytest.approx(
            AUTO_REDISCOVERY_SWEEP_DURATION, abs=0.01
        )
        assert sweep_3 - sweep_2 == pytest.approx(
            AUTO_REDISCOVERY_SWEEP_DURATION, abs=0.01
        )
        # Roughly the configured initial delay from now.
        assert sweep_1 - now == pytest.approx(AUTO_INITIAL_SWEEP_DELAY, abs=1.0)
    finally:
        c1()
        c2()
        c3()


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
async def test_on_advertisement_re_bootstraps_pruned_tracking() -> None:
    """If a tracking entry was pruned, the next ad re-creates it and wakes."""
    manager = get_manager()
    sched = manager._auto_scheduler
    address = "11:22:33:44:55:66"
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    cancel = manager.async_register_active_scan(address, scan_interval=120.0)
    try:
        worker = sched._workers[scanner.source]
        # Simulate the prune-on-no-history step having removed the entry.
        del sched._needs[address]
        worker._wake.clear()
        _inject(scanner, address)
        assert address in sched._needs
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
    with pytest.raises(ValueError, match="scan_duration must be >= 0"):
        manager.async_register_active_scan(
            "AA:BB:CC:DD:EE:00", scan_interval=60.0, scan_duration=-0.5
        )


@pytest.mark.asyncio
async def test_register_active_scan_applies_defaults() -> None:
    """Omitting scan_interval/scan_duration uses the configured defaults."""
    from habluetooth.const import (
        DEFAULT_ACTIVE_SCAN_DURATION,
        DEFAULT_ACTIVE_SCAN_INTERVAL,
    )

    manager = get_manager()
    sched = manager._auto_scheduler
    address = "AA:BB:CC:DD:EE:42"
    cancel = manager.async_register_active_scan(address)
    try:
        request = next(iter(sched._requests_by_address[address]))
        assert request.scan_interval == DEFAULT_ACTIVE_SCAN_INTERVAL
        assert request.scan_duration == DEFAULT_ACTIVE_SCAN_DURATION
    finally:
        cancel()


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


@pytest.mark.asyncio
async def test_next_event_at_returns_earliest_per_device_need() -> None:
    """Per-device entries owned by this scanner influence the next-event time."""
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    address = "11:22:33:44:55:66"
    cancel = manager.async_register_active_scan(address, scan_interval=120.0)
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        _inject(scanner, address)
        worker = sched._workers[scanner.source]
        # Sweep is far in the future (initial delay window). The earliest
        # event for the worker is the per-device next-due.
        entries = sched._needs[address]
        request = next(iter(entries))
        per_device_at = loop.time() + 5.0
        entries[request] = per_device_at
        assert worker._next_event_at(loop.time()) == per_device_at
    finally:
        cancel()
        register_cancel()


@pytest.mark.asyncio
async def test_next_event_at_ignores_empty_or_foreign_entries() -> None:
    """Empty entry dicts and entries owned by other scanners don't lower next-event."""
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        worker = sched._workers[scanner.source]
        # Empty entries: hits the "if not entries: continue" branch.
        sched._needs["aa:bb:cc:dd:ee:01"] = {}
        # No history at all: hits "if history is None or history.source != source".
        cancel = manager.async_register_active_scan(
            "aa:bb:cc:dd:ee:02", scan_interval=60.0
        )
        request = next(iter(sched._requests_by_address["aa:bb:cc:dd:ee:02"]))
        sched._needs["aa:bb:cc:dd:ee:02"] = {request: loop.time() - 1.0}
        next_at = worker._next_event_at(loop.time())
        # With no contributing per-device entries the next event reverts
        # to the sweep cadence (well into the future via initial delay).
        assert next_at == worker._sweep_last_completed + AUTO_REDISCOVERY_INTERVAL
        cancel()
        del sched._needs["aa:bb:cc:dd:ee:01"]
    finally:
        register_cancel()


@pytest.mark.asyncio
async def test_dispatch_per_device_skips_empty_entries() -> None:
    """An address whose entries dict is empty is skipped (no del, no fire)."""
    manager = get_manager()
    sched = manager._auto_scheduler
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        sched._needs["aa:bb:cc:dd:ee:ff"] = {}
        await sched._workers[scanner.source]._tick()
        assert scanner.active_window_calls == []
        del sched._needs["aa:bb:cc:dd:ee:ff"]
    finally:
        register_cancel()


@pytest.mark.asyncio
async def test_dispatch_per_device_skips_not_yet_due() -> None:
    """Entries with future due times don't fire."""
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    address = "11:22:33:44:55:66"
    cancel = manager.async_register_active_scan(address, scan_interval=120.0)
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        _inject(scanner, address)
        # Push the due time far in the future.
        entries = sched._needs[address]
        for request in list(entries):
            entries[request] = loop.time() + 1000.0
        await sched._workers[scanner.source]._tick()
        assert scanner.active_window_calls == []
    finally:
        cancel()
        register_cancel()


@pytest.mark.asyncio
async def test_tick_skips_when_sweep_not_due_and_no_per_device() -> None:
    """No-op tick: no per-device work due, sweep not due either."""
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        worker = sched._workers[scanner.source]
        worker._sweep_last_completed = loop.time()
        await worker._tick()
        assert scanner.active_window_calls == []
    finally:
        register_cancel()


@pytest.mark.asyncio
async def test_worker_tick_no_op_when_loop_detached() -> None:
    """Worker tick exits cleanly if the scheduler's loop is None."""
    manager = get_manager()
    sched = manager._auto_scheduler
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        worker = sched._workers[scanner.source]
        original_loop = sched._loop
        sched._loop = None
        try:
            await worker._tick()
        finally:
            sched._loop = original_loop
        assert scanner.active_window_calls == []
    finally:
        register_cancel()


@pytest.mark.asyncio
async def test_tick_no_op_when_already_inside_window() -> None:
    """A tick that arrives while a window is in flight returns early."""
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        worker = sched._workers[scanner.source]
        # Pretend a window is mid-flight; _tick must defer to that
        # window and not start a new one.
        worker._window_end = loop.time() + 60.0
        await worker._tick()
        assert scanner.active_window_calls == []
    finally:
        register_cancel()


async def _replace_worker_task(worker: object) -> None:
    """Cancel the worker's existing task so a fresh _run() can be tested."""
    task = worker._task  # type: ignore[attr-defined]
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_run_exits_when_scheduler_not_running() -> None:
    """The worker's _run loop exits cleanly when _running is False after a wake."""
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        worker = sched._workers[scanner.source]
        await _replace_worker_task(worker)
        # Put the sweep clock far in the past so _next_event_at returns
        # a time <= now and the wait_for branch is skipped; the loop
        # falls straight through to the "not running" check.
        worker._sweep_last_completed = loop.time() - AUTO_REDISCOVERY_INTERVAL - 1.0
        sched._running = False
        new_task = loop.create_task(worker._run())
        await asyncio.wait_for(new_task, timeout=1.0)
        assert new_task.done() and not new_task.cancelled()
    finally:
        sched._running = True
        register_cancel()


@pytest.mark.asyncio
async def test_run_exits_when_loop_detached() -> None:
    """The worker's _run loop exits when scheduler._loop becomes None."""
    manager = get_manager()
    sched = manager._auto_scheduler
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        worker = sched._workers[scanner.source]
        await _replace_worker_task(worker)
        original_loop = sched._loop
        sched._loop = None
        new_task = asyncio.get_running_loop().create_task(worker._run())
        await asyncio.wait_for(new_task, timeout=1.0)
        assert new_task.done() and not new_task.cancelled()
        sched._loop = original_loop
    finally:
        register_cancel()


@pytest.mark.asyncio
async def test_add_request_without_history_does_not_wake() -> None:
    """When the address has never been seen, add_request is a pure registry op."""
    manager = get_manager()
    sched = manager._auto_scheduler
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        worker = sched._workers[scanner.source]
        worker._wake.clear()
        cancel = manager.async_register_active_scan(
            "AA:AA:AA:AA:AA:AA", scan_interval=60.0
        )
        # No prior advertisement: history is None, so no wake is sent.
        assert not worker._wake.is_set()
        cancel()
    finally:
        register_cancel()


@pytest.mark.asyncio
async def test_remove_request_handles_missing_bucket() -> None:
    """remove_request tolerates a request whose bucket is already gone."""
    manager = get_manager()
    sched = manager._auto_scheduler
    request = ActiveScanRequest("AA:BB:CC:DD:EE:99", 60.0, None)
    # Bucket was never added; remove_request must be a no-op.
    sched.remove_request(request)
    assert "AA:BB:CC:DD:EE:99" not in sched._requests_by_address
    assert "AA:BB:CC:DD:EE:99" not in sched._needs


@pytest.mark.asyncio
async def test_on_advertisement_no_match_no_wake() -> None:
    """An ad whose address has no registered request doesn't add anything."""
    manager = get_manager()
    sched = manager._auto_scheduler
    cancel = manager.async_register_active_scan("11:22:33:44:55:66", scan_interval=60.0)
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        worker = sched._workers[scanner.source]
        worker._wake.clear()
        _inject(scanner, "AA:AA:AA:AA:AA:AA")
        assert "AA:AA:AA:AA:AA:AA" not in sched._needs
        assert not worker._wake.is_set()
    finally:
        cancel()
        register_cancel()


@pytest.mark.asyncio
async def test_on_advertisement_existing_entry_no_extra_wake() -> None:
    """A second ad for a tracked address with multiple requests skips all."""
    manager = get_manager()
    sched = manager._auto_scheduler
    address = "11:22:33:44:55:66"
    # Two registrations for the same address so the for-loop in
    # on_advertisement iterates twice; both requests must be present
    # in _needs after the first inject, so the second inject takes
    # the request-in-existing branch on every iteration and added
    # stays False.
    cancel1 = manager.async_register_active_scan(address, scan_interval=60.0)
    cancel2 = manager.async_register_active_scan(address, scan_interval=120.0)
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        worker = sched._workers[scanner.source]
        _inject(scanner, address)
        worker._wake.clear()
        _inject(scanner, address)
        assert not worker._wake.is_set()
    finally:
        cancel1()
        cancel2()
        register_cancel()


@pytest.mark.asyncio
async def test_on_advertisement_with_all_requests_already_tracked() -> None:
    """Direct exercise of the existing-entries skip path inside the for-loop."""
    manager = get_manager()
    sched = manager._auto_scheduler
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    address = "11:22:33:44:55:66"
    # Build two requests in the registry directly so we know exactly
    # what's in _requests_by_address; pre-populate _needs with both so
    # on_advertisement's for-loop iterates twice and skips both.
    req_a = ActiveScanRequest(address, 60.0, None)
    req_b = ActiveScanRequest(address, 120.0, None)
    sched._requests_by_address[address] = {req_a, req_b}
    sched._needs[address] = {req_a: 0.0, req_b: 0.0}
    try:
        # Drive on_advertisement directly; both requests are present so
        # added stays False and the wake path is skipped.
        si = BluetoothServiceInfoBleak(
            name="x",
            address=address,
            rssi=-50,
            manufacturer_data={},
            service_data={},
            service_uuids=[],
            source=scanner.source,
            device=generate_ble_device(address, "x"),
            advertisement=generate_advertisement_data(local_name="x"),
            connectable=True,
            time=asyncio.get_running_loop().time(),
            tx_power=None,
            raw=None,
        )
        worker = sched._workers[scanner.source]
        worker._wake.clear()
        sched.on_advertisement(si)
        assert not worker._wake.is_set()
        # Sanity: the entries we put in are untouched.
        assert sched._needs[address] == {req_a: 0.0, req_b: 0.0}
    finally:
        sched._requests_by_address.pop(address, None)
        sched._needs.pop(address, None)
        register_cancel()


@pytest.mark.asyncio
async def test_add_request_with_history_wakes_owning_worker() -> None:
    """add_request wakes the worker whose scanner currently sees the address."""
    manager = get_manager()
    sched = manager._auto_scheduler
    address = "11:22:33:44:55:66"
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        # Populate manager._all_history WITHOUT first registering an
        # active scan, so the inject doesn't go through on_advertisement's
        # wake-on-added path. add_request then sees the history entry
        # and fires _wake_worker itself.
        _inject(scanner, address)
        worker = sched._workers[scanner.source]
        worker._wake.clear()
        cancel = manager.async_register_active_scan(address, scan_interval=60.0)
        assert worker._wake.is_set()
        cancel()
    finally:
        register_cancel()


@pytest.mark.asyncio
async def test_next_event_at_skips_per_device_later_than_sweep() -> None:
    """A per-device next-due later than the sweep cadence does not lower next_at."""
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    address = "11:22:33:44:55:66"
    cancel = manager.async_register_active_scan(address, scan_interval=60.0)
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        _inject(scanner, address)
        worker = sched._workers[scanner.source]
        sweep_at = worker._sweep_last_completed + AUTO_REDISCOVERY_INTERVAL
        # Push per-device need past the sweep cadence so the earliest <
        # next_at branch is False inside _next_event_at.
        for req in list(sched._needs[address]):
            sched._needs[address][req] = sweep_at + 100.0
        assert worker._next_event_at(loop.time()) == sweep_at
    finally:
        cancel()
        register_cancel()


@pytest.mark.asyncio
async def test_start_ignores_non_auto_scanner() -> None:
    """A non-AUTO scanner already on the manager doesn't get a worker on start."""
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    auto = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    active = _RecordingAutoScanner("AA:BB:CC:DD:EE:11", BluetoothScanningMode.ACTIVE)
    c_auto = manager.async_register_scanner(auto)
    c_active = manager.async_register_scanner(active)
    try:
        assert active.source not in sched._workers
        # Re-run start() so the False branch (non-AUTO scanner) of the
        # `if scanner.requested_mode is AUTO` check inside start() is hit.
        # First shut down the worker tasks the existing start() already
        # spawned so we don't leak.
        for worker in list(sched._workers.values()):
            await _replace_worker_task(worker)
        sched._workers.clear()
        sched.start(loop)
        assert auto.source in sched._workers
        assert active.source not in sched._workers
    finally:
        c_auto()
        c_active()


@pytest.mark.asyncio
async def test_wake_worker_without_worker_is_no_op() -> None:
    """_wake_worker tolerates being called for an unknown source."""
    manager = get_manager()
    sched = manager._auto_scheduler
    # No scanner registered for this source; should silently no-op.
    sched._wake_worker("AA:AA:AA:AA:AA:AA")


@pytest.mark.asyncio
async def test_coalesce_three_due_uses_max_clamped() -> None:
    """Three due requests on one address fire one window using max duration."""
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    address = "11:22:33:44:55:66"
    c1 = manager.async_register_active_scan(
        address, scan_interval=60.0, scan_duration=2.0
    )
    c2 = manager.async_register_active_scan(
        address, scan_interval=60.0, scan_duration=4.0
    )
    c3 = manager.async_register_active_scan(
        address, scan_interval=60.0, scan_duration=9.0
    )
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        _inject(scanner, address)
        entries = sched._needs[address]
        for req in list(entries):
            entries[req] = loop.time() - 1.0
        await sched._workers[scanner.source]._tick()
        assert scanner.active_window_calls == [9.0]
    finally:
        c1()
        c2()
        c3()
        register_cancel()


@pytest.mark.asyncio
async def test_coalesce_clamps_oversize_request() -> None:
    """A scan_duration above the max is clamped on dispatch."""
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    address = "11:22:33:44:55:66"
    cancel = manager.async_register_active_scan(
        address, scan_interval=60.0, scan_duration=AUTO_WINDOW_MAX_DURATION + 50.0
    )
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        _inject(scanner, address)
        entries = sched._needs[address]
        for req in list(entries):
            entries[req] = loop.time() - 1.0
        await sched._workers[scanner.source]._tick()
        assert scanner.active_window_calls == [AUTO_WINDOW_MAX_DURATION]
    finally:
        cancel()
        register_cancel()


@pytest.mark.asyncio
async def test_coalesce_none_duration_uses_min() -> None:
    """
    An explicit None scan_duration on a request falls back to the minimum.

    Goes around async_register_active_scan (which defaults scan_duration
    to DEFAULT_ACTIVE_SCAN_DURATION) to exercise the None branch of
    _coalesce_duration directly with a hand-built ActiveScanRequest.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    address = "11:22:33:44:55:66"
    request = ActiveScanRequest(address, 60.0, None)
    sched._requests_by_address.setdefault(address, set()).add(request)
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        _inject(scanner, address)
        entries = sched._needs[address]
        for req in list(entries):
            entries[req] = loop.time() - 1.0
        await sched._workers[scanner.source]._tick()
        assert scanner.active_window_calls == [AUTO_WINDOW_MIN_DURATION]
    finally:
        sched.remove_request(request)
        register_cancel()


@pytest.mark.asyncio
async def test_coalesce_only_due_requests_count() -> None:
    """Only the requests that are actually due contribute to coalesced duration."""
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    address = "11:22:33:44:55:66"
    c_short = manager.async_register_active_scan(
        address, scan_interval=60.0, scan_duration=2.0
    )
    c_long = manager.async_register_active_scan(
        address, scan_interval=300.0, scan_duration=20.0
    )
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        _inject(scanner, address)
        entries = sched._needs[address]
        short_req = next(r for r in entries if r.scan_duration == 2.0)
        long_req = next(r for r in entries if r.scan_duration == 20.0)
        # Only the short request is due; the long one is well in the
        # future and must not pull its bigger duration into the window.
        entries[short_req] = loop.time() - 1.0
        entries[long_req] = loop.time() + 200.0
        await sched._workers[scanner.source]._tick()
        assert scanner.active_window_calls == [2.0]
    finally:
        c_short()
        c_long()
        register_cancel()


@pytest.mark.asyncio
async def test_coalesce_distinct_addresses_share_one_window() -> None:
    """Two due addresses on the same scanner share one max-duration window."""
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    addr_a = "11:22:33:44:55:01"
    addr_b = "11:22:33:44:55:02"
    c1 = manager.async_register_active_scan(
        addr_a, scan_interval=60.0, scan_duration=3.0
    )
    c2 = manager.async_register_active_scan(
        addr_b, scan_interval=60.0, scan_duration=7.0
    )
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        _inject(scanner, addr_a)
        _inject(scanner, addr_b)
        for address in (addr_a, addr_b):
            entries = sched._needs[address]
            for req in list(entries):
                entries[req] = loop.time() - 1.0
        await sched._workers[scanner.source]._tick()
        # A single ACTIVE flip covers both devices; the window length is
        # the max of every due request's duration.
        assert scanner.active_window_calls == [7.0]
    finally:
        c1()
        c2()
        register_cancel()


@pytest.mark.asyncio
async def test_tick_combines_due_sweep_and_per_device_into_one_window() -> None:
    """A due sweep + due per-device fold into a single window at max duration."""
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
        for req in list(entries):
            entries[req] = loop.time() - 1.0
        worker = sched._workers[scanner.source]
        worker._sweep_last_completed = loop.time() - AUTO_REDISCOVERY_INTERVAL - 1.0
        await worker._tick()
        # The sweep duration (15s) beats the per-device duration (3s)
        # so the merged window is sized to the sweep.
        assert scanner.active_window_calls == [AUTO_REDISCOVERY_SWEEP_DURATION]
        # Sweep clock advanced.
        assert worker._sweep_last_completed > loop.time() - 1.0
    finally:
        cancel()
        register_cancel()


@pytest.mark.asyncio
async def test_three_inkbirds_share_one_scan() -> None:
    """
    Three Inkbirds at the same 5min / 15s cadence share a single window.

    Each Inkbird has its own address but all three are owned by the same
    scanner and become due at the same time. The worker coalesces every
    due request across all addresses into a single 15s active window, so
    the radio only stops and restarts once per tick.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    addresses = ["C0:01:01:11:11:11", "C0:01:01:22:22:22", "C0:01:01:33:33:33"]
    cancels = [
        manager.async_register_active_scan(
            addr, scan_interval=300.0, scan_duration=15.0
        )
        for addr in addresses
    ]
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        for addr in addresses:
            _inject(scanner, addr)
            entries = sched._needs[addr]
            for req in list(entries):
                entries[req] = loop.time() - 1.0
        await sched._workers[scanner.source]._tick()
        # All three addresses fold into one coalesced 15s window.
        assert scanner.active_window_calls == [15.0]
        # Next-due moved forward by scan_interval for every request.
        for addr in addresses:
            for due in sched._needs[addr].values():
                assert due > loop.time() + 250.0
    finally:
        for cancel in cancels:
            cancel()
        register_cancel()


@pytest.mark.asyncio
async def test_dispatch_coalesces_different_durations_to_max() -> None:
    """Two addresses with different durations fire one window at the max."""
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    addr_short = "11:22:33:44:55:01"
    addr_long = "11:22:33:44:55:02"
    c_short = manager.async_register_active_scan(
        addr_short, scan_interval=60.0, scan_duration=3.0
    )
    c_long = manager.async_register_active_scan(
        addr_long, scan_interval=60.0, scan_duration=12.0
    )
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        for addr in (addr_short, addr_long):
            _inject(scanner, addr)
            entries = sched._needs[addr]
            for req in list(entries):
                entries[req] = loop.time() - 1.0
        await sched._workers[scanner.source]._tick()
        # Single window sized to the larger of the two durations.
        assert scanner.active_window_calls == [12.0]
    finally:
        c_short()
        c_long()
        register_cancel()


@pytest.mark.asyncio
async def test_three_inkbirds_same_address_coalesce_to_one_scan() -> None:
    """
    Three Inkbird-style registrations on the same address share one window.

    Realistic case: three integrations each register their own callback
    for the same Inkbird; the scheduler must NOT fire 3 separate 15s
    windows back-to-back. Instead all three requests coalesce into one
    single 15s window via _coalesce_duration's max-of-durations.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    address = "C0:01:01:11:11:11"
    cancels = [
        manager.async_register_active_scan(
            address, scan_interval=300.0, scan_duration=15.0
        )
        for _ in range(3)
    ]
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        _inject(scanner, address)
        entries = sched._needs[address]
        assert len(entries) == 3
        for req in list(entries):
            entries[req] = loop.time() - 1.0
        await sched._workers[scanner.source]._tick()
        # All three coalesced into a single 15s window.
        assert scanner.active_window_calls == [15.0]
        # Each request's next-due advanced by its own scan_interval.
        for due in entries.values():
            assert due > loop.time() + 250.0
    finally:
        for cancel in cancels:
            cancel()
        register_cancel()


@pytest.mark.asyncio
async def test_three_inkbirds_window_unchanged_after_removal() -> None:
    """
    Removing one of three same-address registrations preserves the window.

    All three asked for the same 15s duration so the coalesced window is
    15s. Cancelling one of them leaves two requests still asking for
    15s; the resulting window must still be 15s, not regress to the
    MIN_DURATION floor.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    address = "C0:01:01:11:11:11"
    cancels = [
        manager.async_register_active_scan(
            address, scan_interval=300.0, scan_duration=15.0
        )
        for _ in range(3)
    ]
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        _inject(scanner, address)
        entries = sched._needs[address]
        assert len(entries) == 3
        # Cancel one of the three; two should remain in both the registry
        # and the _needs tracker.
        cancels.pop()()
        assert len(sched._requests_by_address[address]) == 2
        entries = sched._needs[address]
        assert len(entries) == 2
        for req in list(entries):
            entries[req] = loop.time() - 1.0
        await sched._workers[scanner.source]._tick()
        # Window duration is unchanged because the remaining two still
        # ask for 15s; coalesce takes the max.
        assert scanner.active_window_calls == [15.0]
    finally:
        for cancel in cancels:
            cancel()
        register_cancel()


@pytest.mark.asyncio
async def test_only_owning_scanner_fires_among_four() -> None:
    """
    Of four AUTO scanners, only the one owning the device's history fires.

    The device is injected from one specific scanner so the manager's
    _all_history points at that source. Every worker's _tick runs;
    only the owner produces an active window. The other three scanners
    stay PASSIVE.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    address = "11:22:33:44:55:66"
    cancel = manager.async_register_active_scan(
        address, scan_interval=60.0, scan_duration=5.0
    )
    scanners = [
        _RecordingAutoScanner(f"AA:00:00:00:00:0{n}", BluetoothScanningMode.AUTO)
        for n in range(4)
    ]
    register_cancels = [manager.async_register_scanner(s) for s in scanners]
    try:
        owner = scanners[2]
        _inject(owner, address)
        entries = sched._needs[address]
        for req in list(entries):
            entries[req] = loop.time() - 1.0
        for scanner in scanners:
            await sched._workers[scanner.source]._tick()
        # Only the owning scanner flipped to ACTIVE for the requested 5s.
        assert [s.active_window_calls for s in scanners] == [[], [], [5.0], []]
    finally:
        for c in register_cancels:
            c()
        cancel()


@pytest.mark.asyncio
async def test_add_request_before_start_does_not_seed_needs() -> None:
    """If add_request runs before start() the entry is deferred to advertisement."""
    manager = get_manager()
    sched = manager._auto_scheduler
    address = "BB:00:00:00:00:00"
    original_loop = sched._loop
    sched._loop = None
    try:
        sched.add_request(ActiveScanRequest(address, 60.0, None))
        assert address in sched._requests_by_address
        assert address not in sched._needs
    finally:
        sched._loop = original_loop
        sched._requests_by_address.pop(address, None)


@pytest.mark.asyncio
async def test_add_request_idempotent_keeps_existing_due() -> None:
    """Re-adding the same request preserves its existing next-due time."""
    manager = get_manager()
    sched = manager._auto_scheduler
    address = "BC:00:00:00:00:00"
    request = ActiveScanRequest(address, 60.0, None)
    sched.add_request(request)
    sched._needs[address][request] = 1234.5
    sched.add_request(request)
    assert sched._needs[address][request] == 1234.5
    sched.remove_request(request)


@pytest.mark.asyncio
async def test_run_loop_waits_then_ticks() -> None:
    """The _run loop's wait_for + _tick path is exercised end-to-end."""
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        worker = sched._workers[scanner.source]
        await _replace_worker_task(worker)
        # Sweep ~1ms in the future so _run's wait_for times out quickly
        # and _tick runs once before we shut it down.
        worker._sweep_last_completed = loop.time() - AUTO_REDISCOVERY_INTERVAL + 0.001
        task = loop.create_task(worker._run())
        await asyncio.sleep(0.05)
        assert scanner.active_window_calls == [AUTO_REDISCOVERY_SWEEP_DURATION]
        sched._running = False
        worker._wake.set()
        await asyncio.wait_for(task, timeout=1.0)
    finally:
        sched._running = True
        register_cancel()


@pytest.mark.asyncio
async def test_owner_flip_during_window_does_not_double_fire() -> None:
    """
    If ownership flips to a second scanner mid-window, no duplicate fire.

    Worker A starts its window for address X. While A awaits the radio,
    a new advertisement makes B the owner (B's _all_history.source).
    B's worker wakes and ticks. Because A advanced X's next-due BEFORE
    starting the await, B's _collect_due_buckets sees the entry as not
    yet due and skips it. A finishes alone with one window; B fires
    none.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    address = "11:22:33:44:55:66"
    cancel = manager.async_register_active_scan(
        address, scan_interval=60.0, scan_duration=5.0
    )
    gate = asyncio.Event()
    s_a = _RecordingAutoScanner("AA:00:00:00:00:01", BluetoothScanningMode.AUTO)
    s_a._block_event = gate
    s_b = _RecordingAutoScanner("AA:00:00:00:00:02", BluetoothScanningMode.AUTO)
    c_a = manager.async_register_scanner(s_a)
    c_b = manager.async_register_scanner(s_b)
    try:
        _inject(s_a, address)
        entries = sched._needs[address]
        for req in list(entries):
            entries[req] = loop.time() - 1.0

        # Worker A starts its tick and blocks inside the scanner call.
        t_a = asyncio.create_task(sched._workers[s_a.source]._tick())
        for _ in range(4):
            await asyncio.sleep(0)
        assert s_a.active_window_calls == [5.0]
        # A advanced entries BEFORE the await; verify that.
        for due in entries.values():
            assert due > loop.time() + 50.0

        # Ownership flips to B (a fresh advertisement on B).
        _inject(s_b, address)

        # B's worker ticks. Because the entry is already in the future
        # it must NOT fire a second window.
        await sched._workers[s_b.source]._tick()
        assert s_b.active_window_calls == []

        gate.set()
        await t_a
    finally:
        gate.set()
        c_a()
        c_b()
        cancel()
