"""Tests for the auto-mode active-window scheduler."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from freezegun import freeze_time

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
    DEFAULT_ACTIVE_SCAN_DURATION,
    DEFAULT_ACTIVE_SCAN_INTERVAL,
    DEFAULT_ON_DEMAND_SWEEP_DURATION,
)

from . import generate_advertisement_data, generate_ble_device

if TYPE_CHECKING:
    from collections.abc import Iterable

    from bleak.backends.device import BLEDevice
    from bleak.backends.scanner import AdvertisementData


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
        "11:22:33:44:55:66", scan_interval=120.0, scan_duration=6.0
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
async def test_worker_tick_advances_by_scan_interval_from_window_start() -> None:
    """
    Next-due is window_start + scan_interval, not window_end + scan_interval.

    scan_interval is documented as the cadence between window *starts*.
    The scheduler advances entries from the tick's ``now`` (when the
    window starts) so the effective period is exactly ``scan_interval``;
    advancing from ``window_end`` instead would make the effective
    period ``scan_interval + scan_duration``.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    address = "11:22:33:44:55:77"
    cancel = manager.async_register_active_scan(
        address, scan_interval=120.0, scan_duration=15.0
    )
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        _inject(scanner, address)
        entries = sched._needs[address]
        request = next(iter(entries))
        entries[request] = loop.time() - 1.0
        before_tick = loop.time()
        await _run_worker_tick(sched, scanner.source)
        # entries[request] should be the tick's now + scan_interval ==
        # roughly before_tick + 120. Definitely NOT before_tick + 135
        # (which is what "scan_interval after window ends" would give).
        assert entries[request] == pytest.approx(before_tick + 120.0, abs=0.1)
        assert entries[request] < before_tick + 130.0
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
        address, scan_interval=120.0, scan_duration=6.0
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
        address, scan_interval=60.0, scan_duration=5.0
    )
    cancel_slow = manager.async_register_active_scan(
        address, scan_interval=300.0, scan_duration=7.0
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
        assert scanner.active_window_calls == [5.0]
        assert entries[fast] > loop.time()
        assert entries[slow] > loop.time() + 100
        entries[fast] = loop.time() - 1.0
        entries[slow] = loop.time() - 1.0
        await _run_worker_tick(sched, scanner.source)
        assert scanner.active_window_calls == [5.0, 7.0]
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
        # Each subsequent worker's first sweep is at least one
        # sweep-duration later than the previous one's. The delta is
        # `SWEEP_DURATION + (loop.time() drift between spawn calls)`,
        # so assert the floor rather than equality with a tight
        # tolerance — CI registrations can take >10ms between
        # _spawn_worker calls and would otherwise flake.
        assert sweep_2 - sweep_1 >= AUTO_REDISCOVERY_SWEEP_DURATION
        assert sweep_3 - sweep_2 >= AUTO_REDISCOVERY_SWEEP_DURATION
        # And the drift component stays small — well under a second.
        assert sweep_2 - sweep_1 < AUTO_REDISCOVERY_SWEEP_DURATION + 1.0
        assert sweep_3 - sweep_2 < AUTO_REDISCOVERY_SWEEP_DURATION + 1.0
        # Roughly the configured initial delay from now.
        assert sweep_1 - now == pytest.approx(AUTO_INITIAL_SWEEP_DELAY, abs=1.0)
    finally:
        c1()
        c2()
        c3()


@pytest.mark.asyncio
async def test_first_sweep_stagger_wraps_past_window_size() -> None:
    """
    Past AUTO_INITIAL_SWEEP_DELAY/SWEEP_DURATION scanners, offsets wrap.

    With the modulo cap on the spawn offset, the Nth scanner where
    N == AUTO_INITIAL_SWEEP_DELAY/AUTO_REDISCOVERY_SWEEP_DURATION
    wraps back to offset 0. This locks in the contract that the
    stagger does not grow unboundedly with worker count.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    wrap_at = int(AUTO_INITIAL_SWEEP_DELAY // AUTO_REDISCOVERY_SWEEP_DURATION)
    n = wrap_at + 1  # one past the wrap
    cancels = []
    try:
        for i in range(n):
            s = _RecordingAutoScanner(
                f"AA:BB:CC:00:00:{i:02x}", BluetoothScanningMode.AUTO
            )
            cancels.append(manager.async_register_scanner(s))
        # The Nth scanner's first sweep is wrap_at scanners' worth of
        # offset modulo AUTO_INITIAL_SWEEP_DELAY -> back to 0; the
        # first scanner was also at offset 0, so their next-sweep times
        # match within a small slack for loop.time() advancing.
        workers = list(sched._workers.values())
        first_sweep_a = workers[0]._sweep_last_completed + AUTO_REDISCOVERY_INTERVAL
        first_sweep_wrap = (
            workers[wrap_at]._sweep_last_completed + AUTO_REDISCOVERY_INTERVAL
        )
        assert abs(first_sweep_wrap - first_sweep_a) < 1.0
    finally:
        for c in cancels:
            c()


@pytest.mark.asyncio
async def test_active_scan_registered_before_auto_scanner_wakes_on_register() -> None:
    """
    A request registered before any AUTO scanner exists wakes the right one.

    Sequence: async_register_active_scan (request enters
    _requests_by_address; no worker exists yet for the device).
    Later, an AUTO scanner is registered and starts seeing the device.
    The first advertisement on that scanner must wake its worker so
    the entry in _needs is acted upon.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    address = "11:22:33:44:55:88"
    cancel = manager.async_register_active_scan(address, scan_interval=60.0)
    # Sanity: request is recorded; no worker yet for any source.
    assert address in sched._requests_by_address
    try:
        scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:99", BluetoothScanningMode.AUTO)
        register_cancel = manager.async_register_scanner(scanner)
        try:
            worker = sched._workers[scanner.source]
            worker._wake.clear()
            _inject(scanner, address)
            assert worker._wake.is_set()
            # The address now has a tracked entry on this scanner.
            assert address in sched._needs
        finally:
            register_cancel()
    finally:
        cancel()


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
    cancel = manager.async_register_active_scan("AA:BB:CC:DD:EE:FF", scan_interval=60.0)
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        request = next(iter(sched._requests_by_address["AA:BB:CC:DD:EE:FF"]))
        sched._needs["AA:BB:CC:DD:EE:FF"] = {request: loop.time() - 1.0}
        await _run_worker_tick(sched, scanner.source)
        assert "AA:BB:CC:DD:EE:FF" not in sched._needs
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
async def test_remove_scanner_prunes_owned_needs_entries() -> None:
    """
    _needs entries owned by the leaving scanner are pruned at remove.

    Without the prune, those entries would sit pinned until the
    device either turns up on another scanner (history flips) or
    expires from _all_history.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    address_owned = "AA:00:00:00:00:10"
    address_foreign = "AA:00:00:00:00:11"
    s_a = _RecordingAutoScanner("AA:00:00:00:00:01", BluetoothScanningMode.AUTO)
    s_b = _RecordingAutoScanner("AA:00:00:00:00:02", BluetoothScanningMode.AUTO)
    c_a = manager.async_register_scanner(s_a)
    c_b = manager.async_register_scanner(s_b)
    cancel_owned = manager.async_register_active_scan(
        address_owned, scan_interval=60.0, scan_duration=5.0
    )
    cancel_foreign = manager.async_register_active_scan(
        address_foreign, scan_interval=60.0, scan_duration=5.0
    )
    try:
        _inject(s_a, address_owned)
        _inject(s_b, address_foreign)
        assert address_owned in sched._needs
        assert address_foreign in sched._needs
        # Remove s_a. The owned entry must be pruned; the foreign one
        # (owned by s_b) must remain.
        c_a()
        await asyncio.sleep(0)
        assert address_owned not in sched._needs
        assert address_foreign in sched._needs
    finally:
        cancel_owned()
        cancel_foreign()
        c_b()


@pytest.mark.asyncio
async def test_add_scanner_before_start_defers_worker() -> None:
    """A scanner registered before start() gets its worker on start()."""
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = sched._loop
    assert loop is not None
    sched._loop = None
    sched._running = False
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
async def test_stop_clears_loop_so_post_stop_add_request_is_record_only() -> None:
    """
    After stop(), add_request and on_advertisement skip _needs.

    Without nulling _loop, post-stop add_request would seed _needs
    with timestamps from the cancelled loop and try to wake a worker
    that no longer exists. on_advertisement is similar. Both must
    fall back to the record-only / no-op path once stop() runs.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    address = "AA:BB:CC:DD:EE:90"
    scanner = _RecordingAutoScanner("AA:00:00:00:00:33", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        _inject(scanner, address)  # seed history
        sched.stop()
        assert sched._loop is None
        # add_request after stop: still tracked in _requests_by_address
        # but no _needs seed (loop is None).
        cancel = manager.async_register_active_scan(address, scan_interval=60.0)
        try:
            assert address in sched._requests_by_address
            assert address not in sched._needs
            # on_advertisement after stop is a no-op on _needs too.
            _inject(scanner, address)
            assert address not in sched._needs
        finally:
            cancel()
    finally:
        register_cancel()
        # Restore the scheduler so the conftest teardown isn't surprised
        # by a None loop.
        sched.start(asyncio.get_running_loop())


@pytest.mark.asyncio
async def test_duration_clamped_to_bounds() -> None:
    """_coalesce_duration clamps the requested duration to the configured range."""
    sched = get_manager()._auto_scheduler

    def _req(duration: float) -> ActiveScanRequest:
        return ActiveScanRequest("AA", 60.0, duration)

    assert sched._coalesce_duration([_req(0.01)]) == AUTO_WINDOW_MIN_DURATION
    assert sched._coalesce_duration([_req(1000.0)]) == AUTO_WINDOW_MAX_DURATION
    assert sched._coalesce_duration([_req(7.5)]) == 7.5
    assert sched._coalesce_duration([_req(0.01), _req(7.5)]) == 7.5
    assert (
        sched._coalesce_duration([_req(7.5), _req(1000.0)]) == AUTO_WINDOW_MAX_DURATION
    )
    # Empty list falls back to the configured minimum.
    assert sched._coalesce_duration([]) == AUTO_WINDOW_MIN_DURATION


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
        # No advertisement has been seen yet, so add_request skipped
        # the _needs seed (the prune-on-no-history path). Simulate the
        # "pruned" state by ensuring it's not there.
        sched._needs.pop(address, None)
        worker._wake.clear()
        _inject(scanner, address)
        assert address in sched._needs
        assert worker._wake.is_set()
    finally:
        cancel()
        register_cancel()


@pytest.mark.asyncio
async def test_register_active_scan_validates_inputs() -> None:
    """scan_interval / scan_duration below the configured minimums raise."""
    manager = get_manager()
    # scan_interval below 60s.
    with pytest.raises(ValueError, match="scan_interval must"):
        manager.async_register_active_scan("AA:BB:CC:DD:EE:00", scan_interval=0)
    with pytest.raises(ValueError, match="scan_interval must"):
        manager.async_register_active_scan("AA:BB:CC:DD:EE:00", scan_interval=30.0)
    # scan_duration below 5s.
    with pytest.raises(ValueError, match="scan_duration must"):
        manager.async_register_active_scan(
            "AA:BB:CC:DD:EE:00", scan_interval=60.0, scan_duration=-0.5
        )
    with pytest.raises(ValueError, match="scan_duration must"):
        manager.async_register_active_scan(
            "AA:BB:CC:DD:EE:00", scan_interval=60.0, scan_duration=4.5
        )
    # Empty address.
    with pytest.raises(ValueError, match="address must be a non-empty string"):
        manager.async_register_active_scan("", scan_interval=60.0)
    # Non-finite values must be rejected: NaN compared to anything
    # returns False, so without the explicit isfinite() check a NaN
    # would slip past the lower-bound validators.
    for bad in (math.nan, math.inf, -math.inf):
        with pytest.raises(ValueError, match="scan_interval must be a finite number"):
            manager.async_register_active_scan("AA:BB:CC:DD:EE:00", scan_interval=bad)
        with pytest.raises(ValueError, match="scan_duration must be a finite number"):
            manager.async_register_active_scan(
                "AA:BB:CC:DD:EE:00", scan_interval=60.0, scan_duration=bad
            )


@pytest.mark.asyncio
async def test_register_active_scan_applies_defaults() -> None:
    """Omitting scan_interval/scan_duration uses the configured defaults."""
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
async def test_register_active_scan_uuid_passes_through_unchanged() -> None:
    """
    MacOS CoreBluetooth UUIDs are not uppercased.

    BlueZ / proxy addresses are colon-form MACs and get normalized
    to upper-case; UUIDs (no colons) must pass through unchanged
    because CoreBluetooth preserves case on its source addresses.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    uuid = "abcd1234-5678-90ab-cdef-1234567890ab"
    cancel = manager.async_register_active_scan(uuid, scan_interval=60.0)
    try:
        assert uuid in sched._requests_by_address
        assert uuid.upper() not in sched._requests_by_address
        request = next(iter(sched._requests_by_address[uuid]))
        assert request.address == uuid
    finally:
        cancel()


@pytest.mark.asyncio
async def test_register_active_scan_normalizes_address_case() -> None:
    """
    Lowercase addresses get normalized to the upper-case form.

    Matches the upper-case form BlueZ / bleak use for advertisement
    source addresses so on_advertisement's dict lookup finds the
    request regardless of caller case.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    upper = "AA:BB:CC:DD:EE:55"
    cancel = manager.async_register_active_scan(upper.lower(), scan_interval=60.0)
    try:
        # Stored under the upper-case form, regardless of caller's case.
        assert upper in sched._requests_by_address
        assert upper.lower() not in sched._requests_by_address
        request = next(iter(sched._requests_by_address[upper]))
        assert request.address == upper
    finally:
        cancel()


@pytest.mark.asyncio
async def test_add_request_without_history_skips_seed() -> None:
    """
    add_request skips _needs when no last_service_info exists yet.

    on_advertisement bootstraps tracking instead. The previous
    behavior seeded unconditionally and let the next worker tick
    prune the orphan entry; skipping the seed avoids that churn.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    address = "AA:BB:CC:DD:EE:56"
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    cancel = manager.async_register_active_scan(address, scan_interval=60.0)
    try:
        # Sanity: history doesn't exist for this address yet.
        assert manager.async_last_service_info(address, False) is None
        # _needs was not seeded -> no entry to prune later.
        assert address not in sched._needs
        # But the request IS recorded for on_advertisement to pick up.
        assert address in sched._requests_by_address
        # First advertisement bootstraps tracking and wakes the
        # owner's worker.
        worker = sched._workers[scanner.source]
        worker._wake.clear()
        _inject(scanner, address)
        assert address in sched._needs
        assert worker._wake.is_set()
    finally:
        cancel()
        register_cancel()


@pytest.mark.asyncio
async def test_run_window_swallows_scanner_exception() -> None:
    """An exception from async_request_active_window is logged, not re-raised."""
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()

    class _FailingScanner(_RecordingAutoScanner):
        async def async_request_active_window(self, duration: float) -> bool:
            msg = "boom"
            raise RuntimeError(msg)

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
async def test_repeated_window_failures_log_only_first_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Persistently failing scanner gets one exception log then warnings.

    Without rate-limiting, a permanently broken scanner would emit a
    full traceback every scan_interval (>= 60s). The first failure
    still logs the full stack so the root cause is captured; subsequent
    failures collapse to a one-line warning to avoid flooding the log.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()

    class _FailingScanner(_RecordingAutoScanner):
        async def async_request_active_window(self, duration: float) -> bool:
            msg = "boom"
            raise RuntimeError(msg)

    scanner = _FailingScanner("AA:BB:CC:DD:EE:11", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        worker = sched._workers[scanner.source]
        worker._sweep_last_completed = loop.time() - AUTO_REDISCOVERY_INTERVAL - 1.0
        with caplog.at_level(logging.WARNING, logger="habluetooth.auto_scheduler"):
            await worker._tick()
            # Trigger a second failure.
            worker._sweep_last_completed = loop.time() - AUTO_REDISCOVERY_INTERVAL - 1.0
            await worker._tick()
        records = [
            r for r in caplog.records if "error running active window" in r.message
        ]
        assert len(records) == 2
        # First has exception info (full traceback), second does not.
        assert records[0].exc_info is not None
        assert records[1].exc_info is None
    finally:
        register_cancel()


@pytest.mark.asyncio
async def test_tick_sync_phase_exception_is_logged_and_worker_survives(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Sync-phase failures in _tick are logged; worker survives.

    Stubs async_last_service_info to raise so _collect_due_buckets
    blows up; the outer except in _tick catches it and logs.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    address = "11:22:33:44:55:91"
    cancel = manager.async_register_active_scan(
        address, scan_interval=60.0, scan_duration=5.0
    )
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:31", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        _inject(scanner, address)
        worker = sched._workers[scanner.source]
        original = manager.async_last_service_info

        def _boom(_addr: str, _conn: bool) -> None:
            msg = "boom in last_service_info"
            raise RuntimeError(msg)

        manager.async_last_service_info = _boom  # type: ignore[assignment,method-assign]
        try:
            with caplog.at_level(logging.ERROR):
                await worker._tick()
            assert any(
                "unexpected error in auto-window tick" in record.message
                for record in caplog.records
            )
            # Worker is still alive; _window_end was reset.
            assert worker._window_end == 0.0
        finally:
            manager.async_last_service_info = original  # type: ignore[method-assign]
    finally:
        cancel()
        register_cancel()


@pytest.mark.asyncio
async def test_mode_switch_unregister_then_register_picks_up_existing_request() -> None:
    """
    Scheduler survives a HA-style scanner mode switch on the same source.

    HA's UI mode-switch path reloads the config entry: the old
    scanner is unregistered, a new one with the same source is
    registered with the new mode. The scheduler must (1) prune
    _needs entries the leaving scanner owned via remove_scanner,
    (2) keep user-registered ActiveScanRequests in
    _requests_by_address, (3) spawn a fresh worker for a new AUTO
    scanner via add_scanner, and (4) bootstrap _needs on the first
    advertisement from the new scanner.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    address = "11:22:33:44:55:92"
    # Register the active-scan need first.
    cancel = manager.async_register_active_scan(
        address, scan_interval=60.0, scan_duration=5.0
    )
    # Start in AUTO, see the device, then "switch to ACTIVE".
    auto_scanner = _RecordingAutoScanner(
        "AA:BB:CC:DD:EE:32", BluetoothScanningMode.AUTO
    )
    auto_cancel = manager.async_register_scanner(auto_scanner)
    try:
        _inject(auto_scanner, address)
        assert address in sched._needs
        assert auto_scanner.source in sched._workers
        # Mode switch in UI -> unregister AUTO scanner.
        auto_cancel()
        assert auto_scanner.source not in sched._workers
        assert address not in sched._needs
        # User's registration is preserved across the switch.
        assert address in sched._requests_by_address
        # Re-register with the SAME source but PASSIVE mode.
        passive_scanner = _RecordingAutoScanner(
            "AA:BB:CC:DD:EE:32", BluetoothScanningMode.PASSIVE
        )
        passive_cancel = manager.async_register_scanner(passive_scanner)
        try:
            # PASSIVE doesn't get a worker.
            assert passive_scanner.source not in sched._workers
            # Still no _needs entry (no AUTO scanner owns it).
            assert address not in sched._needs
            passive_cancel()
            # Now switch BACK to AUTO with the same source.
            new_auto = _RecordingAutoScanner(
                "AA:BB:CC:DD:EE:32", BluetoothScanningMode.AUTO
            )
            new_auto_cancel = manager.async_register_scanner(new_auto)
            try:
                assert new_auto.source in sched._workers
                # First advertisement on the new AUTO scanner bootstraps
                # tracking again from the still-registered request.
                _inject(new_auto, address)
                assert address in sched._needs
            finally:
                new_auto_cancel()
        except BaseException:
            passive_cancel()
            raise
    finally:
        cancel()


@pytest.mark.asyncio
async def test_start_replays_pre_start_requests_into_needs() -> None:
    """
    add_request before start() seeds _needs at start() if history exists.

    Also covers the no-history skip path and the
    already-in-existing-entries no-op so the replay loop's branches
    are all exercised.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    address_with_history = "11:22:33:44:55:80"
    address_no_history = "11:22:33:44:55:81"
    # Get history in place for one address only.
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:21", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    _inject(scanner, address_with_history)
    try:
        saved_loop = sched._loop
        assert saved_loop is not None
        sched._loop = None
        sched._running = False
        try:
            # Register TWO requests on the with-history address so
            # we can pre-populate _needs with one of them and prove
            # start() (a) leaves the pre-existing entry alone and
            # (b) inserts a fresh entry for the other.
            cancel_with_a = manager.async_register_active_scan(
                address_with_history, scan_interval=60.0, scan_duration=5.0
            )
            cancel_with_b = manager.async_register_active_scan(
                address_with_history, scan_interval=120.0, scan_duration=5.0
            )
            cancel_without = manager.async_register_active_scan(
                address_no_history, scan_interval=60.0, scan_duration=5.0
            )
            try:
                assert address_with_history not in sched._needs
                requests = list(sched._requests_by_address[address_with_history])
                pre_existing, to_be_inserted = requests
                # Pre-populate _needs with one request only. The
                # sentinel is well above loop.time() + scan_interval
                # so the test is robust against the loop being
                # freshly-started (CI) or long-lived; we don't care
                # about the absolute value, only that start() leaves
                # it alone.
                sentinel = saved_loop.time() + 1.0e9
                sched._needs[address_with_history] = {pre_existing: sentinel}
                before_start = saved_loop.time()
                sched.start(saved_loop)
                seeded = sched._needs[address_with_history]
                # The pre-existing entry was left alone (covers the
                # `request not in existing` False branch).
                assert seeded[pre_existing] == sentinel
                # The other request got freshly inserted (covers the
                # insert line in the replay loop).
                assert to_be_inserted in seeded
                assert seeded[to_be_inserted] == pytest.approx(
                    before_start + to_be_inserted.scan_interval, abs=0.1
                )
                # No-history address: skipped by the
                # `last_service_info(...) is None` branch.
                assert address_no_history not in sched._needs
            finally:
                cancel_with_a()
                cancel_with_b()
                cancel_without()
        finally:
            sched._loop = saved_loop
            sched._running = True
    finally:
        register_cancel()


@pytest.mark.asyncio
async def test_start_is_idempotent_when_already_running() -> None:
    """
    A second start() call without an intervening stop() is a no-op.

    Guards against an accidental double-call binding a different loop
    to the same scheduler or re-running the pre-start replay block.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    # The conftest's async_setup already called start(), so _running
    # is True. A second start with a different loop must NOT replace
    # _loop or re-run anything.
    original_loop = sched._loop
    bogus_loop = object()
    sched.start(bogus_loop)  # type: ignore[arg-type]
    assert sched._loop is original_loop


@pytest.mark.asyncio
async def test_dispatch_does_not_resurrect_cancelled_request() -> None:
    """A request cancelled while the window awaits is not re-added to entries."""
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    address = "11:22:33:44:55:66"
    cancel = manager.async_register_active_scan(
        address, scan_interval=60.0, scan_duration=6.0
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
        sched._needs["AA:BB:CC:DD:EE:01"] = {}
        # No history at all: hits "if history is None or history.source != source".
        cancel = manager.async_register_active_scan(
            "AA:BB:CC:DD:EE:02", scan_interval=60.0
        )
        request = next(iter(sched._requests_by_address["AA:BB:CC:DD:EE:02"]))
        sched._needs["AA:BB:CC:DD:EE:02"] = {request: loop.time() - 1.0}
        next_at = worker._next_event_at(loop.time())
        # With no contributing per-device entries the next event reverts
        # to the sweep cadence (well into the future via initial delay).
        assert next_at == worker._sweep_last_completed + AUTO_REDISCOVERY_INTERVAL
        cancel()
        del sched._needs["AA:BB:CC:DD:EE:01"]
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
        sched._needs["AA:BB:CC:DD:EE:FF"] = {}
        await sched._workers[scanner.source]._tick()
        assert scanner.active_window_calls == []
        del sched._needs["AA:BB:CC:DD:EE:FF"]
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
        assert new_task.done()
        assert not new_task.cancelled()
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
        assert new_task.done()
        assert not new_task.cancelled()
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
    request = ActiveScanRequest("AA:BB:CC:DD:EE:99", 60.0, 10.0)
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
async def test_on_advertisement_wakes_on_every_ad_for_tracked_address() -> None:
    """
    Every ad on a tracked address wakes the source's worker.

    The wake is what makes ownership-flip detection work: when this
    scanner becomes the new owner mid-sleep, the wake forces the
    worker to re-evaluate _next_event_at and pick up the entry that
    is now owned by it.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    address = "11:22:33:44:55:66"
    cancel1 = manager.async_register_active_scan(address, scan_interval=60.0)
    cancel2 = manager.async_register_active_scan(address, scan_interval=120.0)
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        worker = sched._workers[scanner.source]
        _inject(scanner, address)
        worker._wake.clear()
        _inject(scanner, address)
        # Second inject still wakes; the wake is unconditional now so
        # ownership flips on an existing entry are seen by the new
        # owner.
        assert worker._wake.is_set()
    finally:
        cancel1()
        cancel2()
        register_cancel()


@pytest.mark.asyncio
async def test_on_advertisement_with_all_requests_already_tracked() -> None:
    """on_advertisement still wakes when every request is already in _needs."""
    manager = get_manager()
    sched = manager._auto_scheduler
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    address = "11:22:33:44:55:66"
    req_a = ActiveScanRequest(address, 60.0, 10.0)
    req_b = ActiveScanRequest(address, 120.0, 10.0)
    sched._requests_by_address[address] = {req_a, req_b}
    sched._needs[address] = {req_a: 0.0, req_b: 0.0}
    try:
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
        # Wake fires unconditionally so ownership-flip detection still
        # triggers when every request was already in _needs.
        assert worker._wake.is_set()
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
        # spawned so we don't leak. Also flip _running back to False so
        # start()'s idempotency guard lets the re-run through.
        for worker in list(sched._workers.values()):
            await _replace_worker_task(worker)
        sched._workers.clear()
        sched._running = False
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
        address, scan_interval=60.0, scan_duration=5.0
    )
    c2 = manager.async_register_active_scan(
        address, scan_interval=60.0, scan_duration=7.0
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
async def test_coalesce_only_due_requests_count() -> None:
    """Only the requests that are actually due contribute to coalesced duration."""
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    address = "11:22:33:44:55:66"
    c_short = manager.async_register_active_scan(
        address, scan_interval=60.0, scan_duration=5.0
    )
    c_long = manager.async_register_active_scan(
        address, scan_interval=300.0, scan_duration=20.0
    )
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        _inject(scanner, address)
        entries = sched._needs[address]
        short_req = next(r for r in entries if r.scan_duration == 5.0)
        long_req = next(r for r in entries if r.scan_duration == 20.0)
        # Only the short request is due; the long one is well in the
        # future and must not pull its bigger duration into the window.
        entries[short_req] = loop.time() - 1.0
        entries[long_req] = loop.time() + 200.0
        await sched._workers[scanner.source]._tick()
        assert scanner.active_window_calls == [5.0]
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
        addr_a, scan_interval=60.0, scan_duration=6.0
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
        address, scan_interval=60.0, scan_duration=6.0
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
        addr_short, scan_interval=60.0, scan_duration=6.0
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
        sched.add_request(ActiveScanRequest(address, 60.0, 10.0))
        assert address in sched._requests_by_address
        assert address not in sched._needs
    finally:
        sched._loop = original_loop
        sched._requests_by_address.pop(address, None)


@pytest.mark.asyncio
async def test_add_request_idempotent_keeps_existing_due() -> None:
    """
    Re-adding the same request preserves its existing next-due time.

    Also verifies the wake is gated on "actually inserted a new entry":
    a re-register (e.g. an HA config-entry reload) is a no-op on the
    schedule, so the worker should not be woken.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    address = "BC:00:00:00:00:00"
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:42", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        request = ActiveScanRequest(address, 60.0, 10.0)
        sched.add_request(request)
        # Inject so add_request can see history on the second call.
        _inject(scanner, address)
        sched._needs[address][request] = 1234.5
        worker = sched._workers[scanner.source]
        worker._wake.clear()
        sched.add_request(request)
        assert sched._needs[address][request] == 1234.5
        # No new entry → no wake.
        assert not worker._wake.is_set()
        sched.remove_request(request)
    finally:
        register_cancel()


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


def _inject_with_rssi(scanner: _RecordingAutoScanner, address: str, rssi: int) -> None:
    """Drive an advertisement through the scanner with a specific RSSI."""
    adv = generate_advertisement_data(local_name="x", rssi=rssi)
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


@pytest.mark.asyncio
async def test_device_migration_between_scanners_fires_on_new_owner() -> None:
    """
    Migrating from scanner A to B fires the next window on B, not on A.

    Sequence: register active_scan. A sees the device first and becomes
    owner. A's worker fires the first window. The device then comes
    through B with a much stronger RSSI so the manager's
    ADV_RSSI_SWITCH_THRESHOLD flips ownership. Make the entry due
    again and tick both workers: B fires the new window, A skips.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    address = "11:22:33:44:55:99"
    cancel = manager.async_register_active_scan(
        address, scan_interval=60.0, scan_duration=5.0
    )
    s_a = _RecordingAutoScanner("AA:00:00:00:00:01", BluetoothScanningMode.AUTO)
    s_b = _RecordingAutoScanner("AA:00:00:00:00:02", BluetoothScanningMode.AUTO)
    c_a = manager.async_register_scanner(s_a)
    c_b = manager.async_register_scanner(s_b)
    try:
        # A sees the device first; A becomes owner.
        _inject_with_rssi(s_a, address, rssi=-80)
        info_a = manager.async_last_service_info(address, False)
        assert info_a is not None
        assert info_a.source == s_a.source

        # Make the existing tracking entry due and fire the first
        # window on A.
        entries = sched._needs[address]
        for req in list(entries):
            entries[req] = loop.time() - 1.0
        await sched._workers[s_a.source]._tick()
        assert s_a.active_window_calls == [5.0]
        assert s_b.active_window_calls == []

        # Device migrates to B with much stronger signal (delta beats
        # ADV_RSSI_SWITCH_THRESHOLD). The manager flips
        # _all_history.source to B.
        _inject_with_rssi(s_b, address, rssi=-30)
        info_b = manager.async_last_service_info(address, False)
        assert info_b is not None
        assert info_b.source == s_b.source

        # Force the entry due again and run both workers. B (the new
        # owner) fires; A skips because history.source is no longer
        # A's source.
        for req in list(entries):
            entries[req] = loop.time() - 1.0
        await sched._workers[s_a.source]._tick()
        await sched._workers[s_b.source]._tick()
        assert s_a.active_window_calls == [5.0]
        assert s_b.active_window_calls == [5.0]
    finally:
        c_a()
        c_b()
        cancel()


@pytest.mark.asyncio
async def test_device_migration_wakes_new_owner_worker() -> None:
    """
    A fresh advertisement on the new owner wakes its worker.

    Without this wake, a worker that became the owner mid-sleep would
    sit until its previously-computed _next_event_at (sweep cadence)
    even though there's a tracked address whose due time is much
    sooner. The wake is on_advertisement's job and must fire even when
    the _needs entry already exists (i.e. the ad doesn't add a new
    request, it just notifies us this scanner now sees the device).
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    address = "11:22:33:44:55:AA"
    cancel = manager.async_register_active_scan(address, scan_interval=60.0)
    s_a = _RecordingAutoScanner("AA:00:00:00:00:01", BluetoothScanningMode.AUTO)
    s_b = _RecordingAutoScanner("AA:00:00:00:00:02", BluetoothScanningMode.AUTO)
    c_a = manager.async_register_scanner(s_a)
    c_b = manager.async_register_scanner(s_b)
    try:
        # A sees the device first.
        _inject_with_rssi(s_a, address, rssi=-80)
        worker_b = sched._workers[s_b.source]
        worker_b._wake.clear()
        # B sees the device with stronger RSSI and becomes the new
        # owner. B's worker must be woken so it re-evaluates
        # _next_event_at and picks up the existing entry.
        _inject_with_rssi(s_b, address, rssi=-30)
        assert worker_b._wake.is_set()
    finally:
        c_a()
        c_b()
        cancel()


@pytest.mark.asyncio
async def test_stop_clears_needs_so_restart_does_not_reuse_stale_due_times() -> None:
    """
    stop() drops _needs so a later start(new_loop) seeds fresh due-times.

    Without this, a restart against a different event loop (whose
    ``time()`` origin differs) would reuse timestamps from the
    cancelled loop and either fire windows immediately or never.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    address = "11:22:33:44:55:CC"
    cancel = manager.async_register_active_scan(
        address, scan_interval=60.0, scan_duration=5.0
    )
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:CC", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        _inject(scanner, address)
        assert address in sched._needs
        original_loop = sched._loop
        sched.stop()
        # _needs cleared so stale timestamps from the now-defunct loop
        # can't survive into a re-start.
        assert sched._needs == {}
        assert sched._loop is None
        assert sched._workers == {}
        # _requests_by_address is loop-independent and must persist so
        # start() can replay registrations on the new loop.
        assert address in sched._requests_by_address
        # Restart against the same loop; the request gets re-seeded with
        # a fresh due time from the new loop.time() base.
        assert original_loop is not None
        sched.start(original_loop)
        assert address in sched._needs
        entries = sched._needs[address]
        expected_due = original_loop.time() + 60.0
        assert all(abs(due - expected_due) < 0.5 for due in entries.values())
    finally:
        cancel()
        register_cancel()


class _DiscoverableAutoScanner(_RecordingAutoScanner):
    """Recording scanner that reports a configurable discovered set."""

    __slots__ = ("_discovered",)

    def __init__(
        self,
        source: str,
        mode: BluetoothScanningMode | None,
        connectable: bool = True,
    ) -> None:
        super().__init__(source, mode, connectable)
        self._discovered: dict[str, tuple[BLEDevice, AdvertisementData]] = {}

    def add_discovered(self, address: str, rssi: int | None = -60) -> None:
        """Mark ``address`` as currently discovered by this scanner."""
        device = generate_ble_device(address, "x")
        adv = generate_advertisement_data(local_name="x", rssi=rssi)
        self._discovered[address] = (device, adv)

    def get_discovered_device_advertisement_data(
        self, address: str
    ) -> tuple[BLEDevice, AdvertisementData] | None:
        return self._discovered.get(address)


def _make_due(sched: object, address: str) -> None:
    """Make every tracked request for ``address`` due immediately."""
    entries = sched._needs[address]  # type: ignore[attr-defined]
    loop = asyncio.get_running_loop()
    for req in list(entries):
        entries[req] = loop.time() - 1.0


@pytest.mark.asyncio
async def test_worker_tick_delegates_to_fallback_when_owner_is_connecting() -> None:
    """
    Owner mid-connect dispatches the active-window scan to fallback.

    Owner scanner is in the connect-attempt phase
    (``_connections_in_progress() > 0``) so its radio can't service
    the active-window flip. A second AUTO scanner also sees the
    device. The worker for the owner must call
    ``async_request_active_window`` on the fallback, not on the owner.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    address = "11:22:33:44:55:01"
    cancel = manager.async_register_active_scan(
        address, scan_interval=120.0, scan_duration=7.0
    )
    owner = _DiscoverableAutoScanner("AA:00:00:00:01:01", BluetoothScanningMode.AUTO)
    fallback = _DiscoverableAutoScanner("AA:00:00:00:01:02", BluetoothScanningMode.AUTO)
    c_owner = manager.async_register_scanner(owner)
    c_fallback = manager.async_register_scanner(fallback)
    try:
        _inject_with_rssi(owner, address, rssi=-50)
        info = manager.async_last_service_info(address, False)
        assert info is not None
        assert info.source == owner.source
        fallback.add_discovered(address, rssi=-70)
        owner._add_connecting(address)
        _make_due(sched, address)
        await _run_worker_tick(sched, owner.source)
        # Owner can't service the flip; fallback gets the call.
        assert owner.active_window_calls == []
        assert fallback.active_window_calls == [7.0]
    finally:
        owner._finished_connecting(address, connected=False)
        cancel()
        c_owner()
        c_fallback()


@pytest.mark.asyncio
async def test_worker_tick_warns_when_no_fallback_available(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No fallback emits a single WARNING; owner is not flipped."""
    manager = get_manager()
    sched = manager._auto_scheduler
    address = "11:22:33:44:55:02"
    cancel = manager.async_register_active_scan(
        address, scan_interval=120.0, scan_duration=6.0
    )
    owner = _DiscoverableAutoScanner("AA:00:00:00:02:01", BluetoothScanningMode.AUTO)
    c_owner = manager.async_register_scanner(owner)
    try:
        _inject_with_rssi(owner, address, rssi=-50)
        owner._add_connecting(address)
        _make_due(sched, address)
        with caplog.at_level(logging.WARNING, logger="habluetooth.auto_scheduler"):
            await _run_worker_tick(sched, owner.source)
        assert owner.active_window_calls == []
        assert any(
            "no fallback scanner" in record.message and address in record.message
            for record in caplog.records
        )
    finally:
        owner._finished_connecting(address, connected=False)
        cancel()
        c_owner()


@pytest.mark.asyncio
async def test_worker_tick_no_fallback_warning_is_rate_limited(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A second connecting tick with no fallback does not re-warn."""
    manager = get_manager()
    sched = manager._auto_scheduler
    address = "11:22:33:44:55:03"
    cancel = manager.async_register_active_scan(
        address, scan_interval=120.0, scan_duration=6.0
    )
    owner = _DiscoverableAutoScanner("AA:00:00:00:03:01", BluetoothScanningMode.AUTO)
    c_owner = manager.async_register_scanner(owner)
    try:
        _inject_with_rssi(owner, address, rssi=-50)
        owner._add_connecting(address)
        _make_due(sched, address)
        with caplog.at_level(logging.WARNING, logger="habluetooth.auto_scheduler"):
            await _run_worker_tick(sched, owner.source)
            count_after_first = sum(
                1
                for record in caplog.records
                if "no fallback scanner" in record.message
            )
            assert count_after_first == 1
            _make_due(sched, address)
            await _run_worker_tick(sched, owner.source)
            count_after_second = sum(
                1
                for record in caplog.records
                if "no fallback scanner" in record.message
            )
            assert count_after_second == 1
    finally:
        owner._finished_connecting(address, connected=False)
        cancel()
        c_owner()


@pytest.mark.asyncio
async def test_worker_tick_no_fallback_flag_resets_after_recovery(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Successful fallback dispatch re-arms the no-fallback warning."""
    manager = get_manager()
    sched = manager._auto_scheduler
    address = "11:22:33:44:55:04"
    cancel = manager.async_register_active_scan(
        address, scan_interval=120.0, scan_duration=6.0
    )
    owner = _DiscoverableAutoScanner("AA:00:00:00:04:01", BluetoothScanningMode.AUTO)
    fallback = _DiscoverableAutoScanner("AA:00:00:00:04:02", BluetoothScanningMode.AUTO)
    c_owner = manager.async_register_scanner(owner)
    c_fallback = manager.async_register_scanner(fallback)
    try:
        _inject_with_rssi(owner, address, rssi=-50)
        owner._add_connecting(address)
        _make_due(sched, address)
        with caplog.at_level(logging.WARNING, logger="habluetooth.auto_scheduler"):
            # No fallback -> warning.
            await _run_worker_tick(sched, owner.source)
            assert sched._workers[owner.source]._warned_no_fallback is True
            # Fallback appears, dispatch succeeds -> flag clears.
            fallback.add_discovered(address, rssi=-70)
            _make_due(sched, address)
            await _run_worker_tick(sched, owner.source)
            assert fallback.active_window_calls == [6.0]
            assert sched._workers[owner.source]._warned_no_fallback is False
            # Fallback disappears again -> warning fires once more.
            fallback._discovered.clear()
            _make_due(sched, address)
            caplog.clear()
            await _run_worker_tick(sched, owner.source)
            assert any(
                "no fallback scanner" in record.message for record in caplog.records
            )
    finally:
        owner._finished_connecting(address, connected=False)
        cancel()
        c_owner()
        c_fallback()


@pytest.mark.asyncio
async def test_worker_tick_active_scanner_covers_address_no_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    ACTIVE-mode scanner seeing the address counts as scan done.

    The owner is mid-connect, so it can't service the active-window
    flip. Another scanner has ``requested_mode is ACTIVE`` and sees
    the address — by definition that scanner is already actively
    scanning. The dispatch must drop the request silently: no
    warning, no ``async_request_active_window`` call on the ACTIVE
    scanner (which would no-op anyway via the ``requested_mode``
    guard in ``HaScanner.async_request_active_window``).
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    address = "11:22:33:44:55:05"
    cancel = manager.async_register_active_scan(
        address, scan_interval=120.0, scan_duration=6.0
    )
    owner = _DiscoverableAutoScanner("AA:00:00:00:05:01", BluetoothScanningMode.AUTO)
    active = _DiscoverableAutoScanner("AA:00:00:00:05:03", BluetoothScanningMode.ACTIVE)
    c_owner = manager.async_register_scanner(owner)
    c_active = manager.async_register_scanner(active)
    try:
        _inject_with_rssi(owner, address, rssi=-50)
        active.add_discovered(address, rssi=-60)
        owner._add_connecting(address)
        _make_due(sched, address)
        with caplog.at_level(logging.WARNING, logger="habluetooth.auto_scheduler"):
            await _run_worker_tick(sched, owner.source)
        assert owner.active_window_calls == []
        assert active.active_window_calls == []
        assert not any(
            "no fallback scanner" in record.message for record in caplog.records
        )
        assert sched._workers[owner.source]._warned_no_fallback is False
    finally:
        owner._finished_connecting(address, connected=False)
        cancel()
        c_owner()
        c_active()


@pytest.mark.asyncio
async def test_worker_tick_active_coverage_preferred_over_auto_fallback() -> None:
    """When ACTIVE covers, no AUTO-fallback flip is needed either."""
    manager = get_manager()
    sched = manager._auto_scheduler
    address = "11:22:33:44:55:0C"
    cancel = manager.async_register_active_scan(
        address, scan_interval=120.0, scan_duration=6.0
    )
    owner = _DiscoverableAutoScanner("AA:00:00:00:0C:01", BluetoothScanningMode.AUTO)
    auto_fb = _DiscoverableAutoScanner("AA:00:00:00:0C:02", BluetoothScanningMode.AUTO)
    active = _DiscoverableAutoScanner("AA:00:00:00:0C:03", BluetoothScanningMode.ACTIVE)
    c_owner = manager.async_register_scanner(owner)
    c_auto = manager.async_register_scanner(auto_fb)
    c_active = manager.async_register_scanner(active)
    try:
        _inject_with_rssi(owner, address, rssi=-50)
        auto_fb.add_discovered(address, rssi=-55)
        active.add_discovered(address, rssi=-70)
        owner._add_connecting(address)
        _make_due(sched, address)
        await _run_worker_tick(sched, owner.source)
        # Covered by ACTIVE: no flip needed on AUTO fallback either.
        assert owner.active_window_calls == []
        assert auto_fb.active_window_calls == []
        assert active.active_window_calls == []
    finally:
        owner._finished_connecting(address, connected=False)
        cancel()
        c_owner()
        c_auto()
        c_active()


@pytest.mark.asyncio
async def test_worker_tick_passive_only_fallback_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Only PASSIVE scanners around: no flip possible, must warn.

    A PASSIVE scanner refuses
    ``async_request_active_window`` and isn't actively scanning, so
    the active scan is truly deferred until the owner's connect
    completes — the warning must fire.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    address = "11:22:33:44:55:0D"
    cancel = manager.async_register_active_scan(
        address, scan_interval=120.0, scan_duration=6.0
    )
    owner = _DiscoverableAutoScanner("AA:00:00:00:0D:01", BluetoothScanningMode.AUTO)
    passive = _DiscoverableAutoScanner(
        "AA:00:00:00:0D:02", BluetoothScanningMode.PASSIVE
    )
    c_owner = manager.async_register_scanner(owner)
    c_passive = manager.async_register_scanner(passive)
    try:
        _inject_with_rssi(owner, address, rssi=-50)
        passive.add_discovered(address, rssi=-60)
        owner._add_connecting(address)
        _make_due(sched, address)
        with caplog.at_level(logging.WARNING, logger="habluetooth.auto_scheduler"):
            await _run_worker_tick(sched, owner.source)
        assert owner.active_window_calls == []
        assert passive.active_window_calls == []
        assert any(
            "no fallback scanner" in record.message and address in record.message
            for record in caplog.records
        )
    finally:
        owner._finished_connecting(address, connected=False)
        cancel()
        c_owner()
        c_passive()


@pytest.mark.asyncio
async def test_worker_tick_dispatch_never_calls_same_fallback_twice() -> None:
    """
    Per-tick dispatch never calls the same fallback more than once.

    Same-tick coalescing guarantees we don't simultaneously trigger
    ``async_request_active_window`` twice on one scanner from a
    single owner's tick. (Cross-tick concurrency between distinct
    owner workers delegating to the same fallback is handled inside
    the scanner: ``HaScanner.async_request_active_window`` extends an
    open active-window timer instead of stopping and restarting the
    radio, and the actual stop/start is serialized by
    ``_start_stop_lock``.)
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    addresses = ("11:22:33:44:55:0E", "11:22:33:44:55:0F", "11:22:33:44:55:10")
    cancels = [
        manager.async_register_active_scan(addr, scan_interval=120.0, scan_duration=6.0)
        for addr in addresses
    ]
    owner = _DiscoverableAutoScanner("AA:00:00:00:0E:01", BluetoothScanningMode.AUTO)
    fb = _DiscoverableAutoScanner("AA:00:00:00:0E:02", BluetoothScanningMode.AUTO)
    c_owner = manager.async_register_scanner(owner)
    c_fb = manager.async_register_scanner(fb)
    try:
        for addr in addresses:
            _inject_with_rssi(owner, addr, rssi=-50)
            fb.add_discovered(addr, rssi=-60)
        owner._add_connecting(addresses[0])
        for addr in addresses:
            _make_due(sched, addr)
        await _run_worker_tick(sched, owner.source)
        # One coalesced call regardless of how many addresses route to fb.
        assert len(fb.active_window_calls) == 1
        assert owner.active_window_calls == []
    finally:
        owner._finished_connecting(addresses[0], connected=False)
        for cancel in cancels:
            cancel()
        c_owner()
        c_fb()


@pytest.mark.asyncio
async def test_worker_tick_active_covers_one_address_warns_for_other(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Per-address classification: covered for one, warn for another.

    Owner is mid-connect with two due addresses. Address A is covered
    by an ACTIVE scanner; address B has no fallback. We expect:
    silent skip for A, warning for B that names only B.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    addr_covered = "11:22:33:44:55:11"
    addr_orphan = "11:22:33:44:55:12"
    c1 = manager.async_register_active_scan(
        addr_covered, scan_interval=120.0, scan_duration=6.0
    )
    c2 = manager.async_register_active_scan(
        addr_orphan, scan_interval=120.0, scan_duration=6.0
    )
    owner = _DiscoverableAutoScanner("AA:00:00:00:11:01", BluetoothScanningMode.AUTO)
    active = _DiscoverableAutoScanner("AA:00:00:00:11:02", BluetoothScanningMode.ACTIVE)
    c_owner = manager.async_register_scanner(owner)
    c_active = manager.async_register_scanner(active)
    try:
        _inject_with_rssi(owner, addr_covered, rssi=-50)
        _inject_with_rssi(owner, addr_orphan, rssi=-50)
        active.add_discovered(addr_covered, rssi=-60)
        # Note: ACTIVE scanner does NOT see addr_orphan.
        owner._add_connecting(addr_covered)
        _make_due(sched, addr_covered)
        _make_due(sched, addr_orphan)
        with caplog.at_level(logging.WARNING, logger="habluetooth.auto_scheduler"):
            await _run_worker_tick(sched, owner.source)
        warnings_for_orphan = [
            record
            for record in caplog.records
            if "no fallback scanner" in record.message and addr_orphan in record.message
        ]
        assert len(warnings_for_orphan) == 1
        # The covered address must not appear in any no-fallback
        # warning text.
        assert not any(
            "no fallback scanner" in record.message and addr_covered in record.message
            for record in caplog.records
        )
        assert active.active_window_calls == []
        assert owner.active_window_calls == []
    finally:
        owner._finished_connecting(addr_covered, connected=False)
        c1()
        c2()
        c_owner()
        c_active()


@pytest.mark.asyncio
async def test_worker_tick_skips_fallback_that_is_also_connecting() -> None:
    """A candidate fallback that's mid-connect is also excluded."""
    manager = get_manager()
    sched = manager._auto_scheduler
    address = "11:22:33:44:55:06"
    cancel = manager.async_register_active_scan(
        address, scan_interval=120.0, scan_duration=6.0
    )
    owner = _DiscoverableAutoScanner("AA:00:00:00:06:01", BluetoothScanningMode.AUTO)
    busy_fb = _DiscoverableAutoScanner("AA:00:00:00:06:02", BluetoothScanningMode.AUTO)
    c_owner = manager.async_register_scanner(owner)
    c_busy = manager.async_register_scanner(busy_fb)
    try:
        _inject_with_rssi(owner, address, rssi=-50)
        busy_fb.add_discovered(address, rssi=-55)
        owner._add_connecting(address)
        busy_fb._add_connecting("AA:BB:CC:DD:EE:FF")
        _make_due(sched, address)
        await _run_worker_tick(sched, owner.source)
        assert owner.active_window_calls == []
        assert busy_fb.active_window_calls == []
    finally:
        owner._finished_connecting(address, connected=False)
        busy_fb._finished_connecting("AA:BB:CC:DD:EE:FF", connected=False)
        cancel()
        c_owner()
        c_busy()


@pytest.mark.asyncio
async def test_worker_tick_fallback_picks_highest_rssi() -> None:
    """When multiple AUTO fallbacks see the device, highest RSSI wins."""
    manager = get_manager()
    sched = manager._auto_scheduler
    address = "11:22:33:44:55:07"
    cancel = manager.async_register_active_scan(
        address, scan_interval=120.0, scan_duration=6.0
    )
    owner = _DiscoverableAutoScanner("AA:00:00:00:07:01", BluetoothScanningMode.AUTO)
    weak = _DiscoverableAutoScanner("AA:00:00:00:07:02", BluetoothScanningMode.AUTO)
    strong = _DiscoverableAutoScanner("AA:00:00:00:07:03", BluetoothScanningMode.AUTO)
    c_owner = manager.async_register_scanner(owner)
    c_weak = manager.async_register_scanner(weak)
    c_strong = manager.async_register_scanner(strong)
    try:
        _inject_with_rssi(owner, address, rssi=-50)
        weak.add_discovered(address, rssi=-90)
        strong.add_discovered(address, rssi=-40)
        owner._add_connecting(address)
        _make_due(sched, address)
        await _run_worker_tick(sched, owner.source)
        assert strong.active_window_calls == [6.0]
        assert weak.active_window_calls == []
    finally:
        owner._finished_connecting(address, connected=False)
        cancel()
        c_owner()
        c_weak()
        c_strong()


@pytest.mark.asyncio
async def test_worker_tick_groups_addresses_by_fallback() -> None:
    """Two due addresses sharing one fallback coalesce to one call."""
    manager = get_manager()
    sched = manager._auto_scheduler
    addr_a = "11:22:33:44:55:08"
    addr_b = "11:22:33:44:55:09"
    cancel_a = manager.async_register_active_scan(
        addr_a, scan_interval=120.0, scan_duration=6.0
    )
    cancel_b = manager.async_register_active_scan(
        addr_b, scan_interval=120.0, scan_duration=11.0
    )
    owner = _DiscoverableAutoScanner("AA:00:00:00:08:01", BluetoothScanningMode.AUTO)
    fb = _DiscoverableAutoScanner("AA:00:00:00:08:02", BluetoothScanningMode.AUTO)
    c_owner = manager.async_register_scanner(owner)
    c_fb = manager.async_register_scanner(fb)
    try:
        _inject_with_rssi(owner, addr_a, rssi=-50)
        _inject_with_rssi(owner, addr_b, rssi=-50)
        fb.add_discovered(addr_a, rssi=-60)
        fb.add_discovered(addr_b, rssi=-60)
        owner._add_connecting(addr_a)
        _make_due(sched, addr_a)
        _make_due(sched, addr_b)
        await _run_worker_tick(sched, owner.source)
        # One coalesced call to the shared fallback with the max duration.
        assert fb.active_window_calls == [11.0]
        assert owner.active_window_calls == []
    finally:
        owner._finished_connecting(addr_a, connected=False)
        cancel_a()
        cancel_b()
        c_owner()
        c_fb()


@pytest.mark.asyncio
async def test_worker_tick_defers_sweep_when_owner_is_connecting() -> None:
    """
    Sweep is per-scanner; defer when connecting rather than spinning.

    With no per-device buckets but sweep_due True, the connecting
    branch must not call ``async_request_active_window`` on the owner.
    It must also advance ``_sweep_last_completed`` so the next worker
    tick re-evaluates in roughly ``_AUTO_CONNECTING_DEFER`` seconds
    rather than firing immediately.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    owner = _DiscoverableAutoScanner("AA:00:00:00:09:01", BluetoothScanningMode.AUTO)
    c_owner = manager.async_register_scanner(owner)
    try:
        worker = sched._workers[owner.source]
        # Force sweep due: place _sweep_last_completed safely in the
        # past so now > _sweep_last_completed + AUTO_REDISCOVERY_INTERVAL.
        worker._sweep_last_completed = loop.time() - AUTO_REDISCOVERY_INTERVAL - 1.0
        owner._add_connecting("11:22:33:44:55:0A")
        before = loop.time()
        await worker._tick()
        assert owner.active_window_calls == []
        # Next-due time should be roughly now + _AUTO_CONNECTING_DEFER,
        # i.e. _sweep_last_completed + AUTO_REDISCOVERY_INTERVAL >=
        # before + (_AUTO_CONNECTING_DEFER - epsilon).
        next_sweep_due = worker._sweep_last_completed + AUTO_REDISCOVERY_INTERVAL
        assert next_sweep_due >= before + 25.0
        assert next_sweep_due <= loop.time() + 60.0
    finally:
        owner._finished_connecting("11:22:33:44:55:0A", connected=False)
        c_owner()


@pytest.mark.asyncio
async def test_worker_tick_skips_fallback_when_owner_is_connected_not_connecting() -> (
    None
):
    """A fully-connected (not connecting) owner still fires its own window."""
    manager = get_manager()
    sched = manager._auto_scheduler
    address = "11:22:33:44:55:0B"
    cancel = manager.async_register_active_scan(
        address, scan_interval=120.0, scan_duration=6.0
    )
    owner = _DiscoverableAutoScanner("AA:00:00:00:0B:01", BluetoothScanningMode.AUTO)
    fb = _DiscoverableAutoScanner("AA:00:00:00:0B:02", BluetoothScanningMode.AUTO)
    c_owner = manager.async_register_scanner(owner)
    c_fb = manager.async_register_scanner(fb)
    try:
        _inject_with_rssi(owner, address, rssi=-50)
        fb.add_discovered(address, rssi=-60)
        # No _add_connecting on the owner: connect has either not
        # started or has already completed. The owner is responsible
        # for the window; fallback stays silent.
        _make_due(sched, address)
        await _run_worker_tick(sched, owner.source)
        assert owner.active_window_calls == [6.0]
        assert fb.active_window_calls == []
    finally:
        cancel()
        c_owner()
        c_fb()


@pytest.mark.asyncio
async def test_worker_tick_fallback_exception_does_not_block_others(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    A raising fallback gets logged; remaining fallbacks still run.

    Two due addresses route to two different fallbacks. The first
    fallback's ``async_request_active_window`` raises. The dispatch
    must still call the second fallback and the worker must remain
    alive.
    """

    class _RaisingScanner(_DiscoverableAutoScanner):
        async def async_request_active_window(self, duration: float) -> bool:
            self.active_window_calls.append(duration)
            msg = "boom"
            raise RuntimeError(msg)

    manager = get_manager()
    sched = manager._auto_scheduler
    addr_a = "11:22:33:44:55:13"
    addr_b = "11:22:33:44:55:14"
    c1 = manager.async_register_active_scan(
        addr_a, scan_interval=120.0, scan_duration=6.0
    )
    c2 = manager.async_register_active_scan(
        addr_b, scan_interval=120.0, scan_duration=6.0
    )
    owner = _DiscoverableAutoScanner("AA:00:00:00:13:01", BluetoothScanningMode.AUTO)
    fb_bad = _RaisingScanner("AA:00:00:00:13:02", BluetoothScanningMode.AUTO)
    fb_good = _DiscoverableAutoScanner("AA:00:00:00:13:03", BluetoothScanningMode.AUTO)
    c_owner = manager.async_register_scanner(owner)
    c_bad = manager.async_register_scanner(fb_bad)
    c_good = manager.async_register_scanner(fb_good)
    try:
        _inject_with_rssi(owner, addr_a, rssi=-50)
        _inject_with_rssi(owner, addr_b, rssi=-50)
        # Only fb_bad sees addr_a; only fb_good sees addr_b. So each
        # address routes to a different fallback.
        fb_bad.add_discovered(addr_a, rssi=-60)
        fb_good.add_discovered(addr_b, rssi=-60)
        owner._add_connecting(addr_a)
        _make_due(sched, addr_a)
        _make_due(sched, addr_b)
        with caplog.at_level(logging.ERROR, logger="habluetooth.auto_scheduler"):
            await _run_worker_tick(sched, owner.source)
        assert fb_bad.active_window_calls == [6.0]
        assert fb_good.active_window_calls == [6.0]
        assert any(
            "error dispatching fallback active window" in record.message
            and fb_bad.name in record.message
            for record in caplog.records
        )
    finally:
        owner._finished_connecting(addr_a, connected=False)
        c1()
        c2()
        c_owner()
        c_bad()
        c_good()


@pytest.mark.asyncio
async def test_worker_tick_sweep_and_per_device_both_handled_when_connecting() -> None:
    """
    Mixed tick: per-device dispatched to fallback AND sweep deferred.

    Sweep is due AND a per-device window is due AND the owner is
    mid-connect. The per-device flip lands on the fallback; the sweep
    is deferred (no flip on the owner) — both behaviors coexist.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    address = "11:22:33:44:55:15"
    cancel = manager.async_register_active_scan(
        address, scan_interval=120.0, scan_duration=6.0
    )
    owner = _DiscoverableAutoScanner("AA:00:00:00:15:01", BluetoothScanningMode.AUTO)
    fb = _DiscoverableAutoScanner("AA:00:00:00:15:02", BluetoothScanningMode.AUTO)
    c_owner = manager.async_register_scanner(owner)
    c_fb = manager.async_register_scanner(fb)
    try:
        _inject_with_rssi(owner, address, rssi=-50)
        fb.add_discovered(address, rssi=-60)
        owner._add_connecting(address)
        worker = sched._workers[owner.source]
        worker._sweep_last_completed = loop.time() - AUTO_REDISCOVERY_INTERVAL - 1.0
        _make_due(sched, address)
        before = loop.time()
        await worker._tick()
        # Per-device went to fallback.
        assert fb.active_window_calls == [6.0]
        assert owner.active_window_calls == []
        # Sweep was deferred (next due roughly now + _AUTO_CONNECTING_DEFER).
        next_sweep_due = worker._sweep_last_completed + AUTO_REDISCOVERY_INTERVAL
        assert next_sweep_due >= before + 25.0
    finally:
        owner._finished_connecting(address, connected=False)
        cancel()
        c_owner()
        c_fb()


@pytest.mark.asyncio
async def test_worker_tick_two_different_fallbacks_both_dispatched() -> None:
    """
    Two due addresses with distinct fallbacks → both get called.

    Confirms that the per-fallback grouping does *not* collapse
    different fallbacks into one — each fallback receives its own
    coalesced ``async_request_active_window`` call.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    addr_a = "11:22:33:44:55:16"
    addr_b = "11:22:33:44:55:17"
    c1 = manager.async_register_active_scan(
        addr_a, scan_interval=120.0, scan_duration=6.0
    )
    c2 = manager.async_register_active_scan(
        addr_b, scan_interval=120.0, scan_duration=8.0
    )
    owner = _DiscoverableAutoScanner("AA:00:00:00:16:01", BluetoothScanningMode.AUTO)
    fb_a = _DiscoverableAutoScanner("AA:00:00:00:16:02", BluetoothScanningMode.AUTO)
    fb_b = _DiscoverableAutoScanner("AA:00:00:00:16:03", BluetoothScanningMode.AUTO)
    c_owner = manager.async_register_scanner(owner)
    c_a = manager.async_register_scanner(fb_a)
    c_b = manager.async_register_scanner(fb_b)
    try:
        _inject_with_rssi(owner, addr_a, rssi=-50)
        _inject_with_rssi(owner, addr_b, rssi=-50)
        fb_a.add_discovered(addr_a, rssi=-60)
        fb_b.add_discovered(addr_b, rssi=-60)
        owner._add_connecting(addr_a)
        _make_due(sched, addr_a)
        _make_due(sched, addr_b)
        await _run_worker_tick(sched, owner.source)
        assert fb_a.active_window_calls == [6.0]
        assert fb_b.active_window_calls == [8.0]
        assert owner.active_window_calls == []
    finally:
        owner._finished_connecting(addr_a, connected=False)
        c1()
        c2()
        c_owner()
        c_a()
        c_b()


@pytest.mark.asyncio
async def test_worker_tick_advance_pre_dispatch_blocks_double_fire() -> None:
    """
    Per-address ``_needs`` entries are advanced before the dispatch.

    The pre-dispatch advance protects against an in-flight ownership
    flip causing a duplicate window on a different worker (same
    reasoning as the non-connecting path's pre-await advance).
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    address = "11:22:33:44:55:18"
    cancel = manager.async_register_active_scan(
        address, scan_interval=90.0, scan_duration=6.0
    )
    owner = _DiscoverableAutoScanner("AA:00:00:00:18:01", BluetoothScanningMode.AUTO)
    fb = _DiscoverableAutoScanner("AA:00:00:00:18:02", BluetoothScanningMode.AUTO)
    c_owner = manager.async_register_scanner(owner)
    c_fb = manager.async_register_scanner(fb)
    try:
        _inject_with_rssi(owner, address, rssi=-50)
        fb.add_discovered(address, rssi=-60)
        owner._add_connecting(address)
        _make_due(sched, address)
        before = loop.time()
        await _run_worker_tick(sched, owner.source)
        entries = sched._needs[address]
        for due in entries.values():
            # Advanced to roughly before + 90s, NOT before - 1.0.
            assert due == pytest.approx(before + 90.0, abs=0.5)
            assert due > loop.time()
    finally:
        owner._finished_connecting(address, connected=False)
        cancel()
        c_owner()
        c_fb()


@pytest.mark.asyncio
async def test_worker_tick_passive_plus_auto_uses_auto(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    PASSIVE + AUTO mix: AUTO is used, PASSIVE ignored, no warning.

    Confirms that a PASSIVE scanner alongside a viable AUTO fallback
    doesn't poison the result — we ignore PASSIVE and flip the AUTO.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    address = "11:22:33:44:55:19"
    cancel = manager.async_register_active_scan(
        address, scan_interval=120.0, scan_duration=6.0
    )
    owner = _DiscoverableAutoScanner("AA:00:00:00:19:01", BluetoothScanningMode.AUTO)
    passive = _DiscoverableAutoScanner(
        "AA:00:00:00:19:02", BluetoothScanningMode.PASSIVE
    )
    auto_fb = _DiscoverableAutoScanner("AA:00:00:00:19:03", BluetoothScanningMode.AUTO)
    c_owner = manager.async_register_scanner(owner)
    c_pass = manager.async_register_scanner(passive)
    c_auto = manager.async_register_scanner(auto_fb)
    try:
        _inject_with_rssi(owner, address, rssi=-50)
        # Passive has a much stronger RSSI to confirm we still ignore it.
        passive.add_discovered(address, rssi=-30)
        auto_fb.add_discovered(address, rssi=-70)
        owner._add_connecting(address)
        _make_due(sched, address)
        with caplog.at_level(logging.WARNING, logger="habluetooth.auto_scheduler"):
            await _run_worker_tick(sched, owner.source)
        assert owner.active_window_calls == []
        assert passive.active_window_calls == []
        assert auto_fb.active_window_calls == [6.0]
        assert not any(
            "no fallback scanner" in record.message for record in caplog.records
        )
    finally:
        owner._finished_connecting(address, connected=False)
        cancel()
        c_owner()
        c_pass()
        c_auto()


@pytest.mark.asyncio
async def test_worker_tick_passive_plus_active_active_covers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    PASSIVE + ACTIVE mix: ACTIVE covers, PASSIVE ignored, no warning.

    No AUTO fallback exists, but an ACTIVE scanner sees the address —
    that's enough for "scan already in progress". The PASSIVE scanner
    is irrelevant.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    address = "11:22:33:44:55:1A"
    cancel = manager.async_register_active_scan(
        address, scan_interval=120.0, scan_duration=6.0
    )
    owner = _DiscoverableAutoScanner("AA:00:00:00:1A:01", BluetoothScanningMode.AUTO)
    passive = _DiscoverableAutoScanner(
        "AA:00:00:00:1A:02", BluetoothScanningMode.PASSIVE
    )
    active = _DiscoverableAutoScanner("AA:00:00:00:1A:03", BluetoothScanningMode.ACTIVE)
    c_owner = manager.async_register_scanner(owner)
    c_pass = manager.async_register_scanner(passive)
    c_active = manager.async_register_scanner(active)
    try:
        _inject_with_rssi(owner, address, rssi=-50)
        passive.add_discovered(address, rssi=-40)
        active.add_discovered(address, rssi=-70)
        owner._add_connecting(address)
        _make_due(sched, address)
        with caplog.at_level(logging.WARNING, logger="habluetooth.auto_scheduler"):
            await _run_worker_tick(sched, owner.source)
        assert owner.active_window_calls == []
        assert passive.active_window_calls == []
        assert active.active_window_calls == []
        assert not any(
            "no fallback scanner" in record.message for record in caplog.records
        )
    finally:
        owner._finished_connecting(address, connected=False)
        cancel()
        c_owner()
        c_pass()
        c_active()


@pytest.mark.asyncio
async def test_worker_tick_all_three_modes_active_wins(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    PASSIVE + ACTIVE + AUTO all present: ACTIVE covers, no flip needed.

    The dispatch must short-circuit on the ACTIVE coverage even when
    an AUTO fallback is also available. PASSIVE is ignored.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    address = "11:22:33:44:55:1B"
    cancel = manager.async_register_active_scan(
        address, scan_interval=120.0, scan_duration=6.0
    )
    owner = _DiscoverableAutoScanner("AA:00:00:00:1B:01", BluetoothScanningMode.AUTO)
    passive = _DiscoverableAutoScanner(
        "AA:00:00:00:1B:02", BluetoothScanningMode.PASSIVE
    )
    active = _DiscoverableAutoScanner("AA:00:00:00:1B:03", BluetoothScanningMode.ACTIVE)
    auto_fb = _DiscoverableAutoScanner("AA:00:00:00:1B:04", BluetoothScanningMode.AUTO)
    c_owner = manager.async_register_scanner(owner)
    c_pass = manager.async_register_scanner(passive)
    c_active = manager.async_register_scanner(active)
    c_auto = manager.async_register_scanner(auto_fb)
    try:
        _inject_with_rssi(owner, address, rssi=-50)
        passive.add_discovered(address, rssi=-40)
        active.add_discovered(address, rssi=-70)
        auto_fb.add_discovered(address, rssi=-55)
        owner._add_connecting(address)
        _make_due(sched, address)
        with caplog.at_level(logging.WARNING, logger="habluetooth.auto_scheduler"):
            await _run_worker_tick(sched, owner.source)
        # ACTIVE covers → no flip on anyone.
        assert owner.active_window_calls == []
        assert passive.active_window_calls == []
        assert active.active_window_calls == []
        assert auto_fb.active_window_calls == []
        assert not any(
            "no fallback scanner" in record.message for record in caplog.records
        )
    finally:
        owner._finished_connecting(address, connected=False)
        cancel()
        c_owner()
        c_pass()
        c_active()
        c_auto()


@pytest.mark.asyncio
async def test_worker_tick_three_way_mix_per_address(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Three addresses, three outcomes in one tick: covered, flipped, warned.

    * addr_covered: only ACTIVE sees → covered, no flip, no warning.
    * addr_flipped: only AUTO sees → flipped on AUTO fallback.
    * addr_orphan: no fallback at all → single warning naming addr_orphan.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    addr_covered = "11:22:33:44:55:1C"
    addr_flipped = "11:22:33:44:55:1D"
    addr_orphan = "11:22:33:44:55:1E"
    c1 = manager.async_register_active_scan(
        addr_covered, scan_interval=120.0, scan_duration=6.0
    )
    c2 = manager.async_register_active_scan(
        addr_flipped, scan_interval=120.0, scan_duration=6.0
    )
    c3 = manager.async_register_active_scan(
        addr_orphan, scan_interval=120.0, scan_duration=6.0
    )
    owner = _DiscoverableAutoScanner("AA:00:00:00:1C:01", BluetoothScanningMode.AUTO)
    active = _DiscoverableAutoScanner("AA:00:00:00:1C:02", BluetoothScanningMode.ACTIVE)
    auto_fb = _DiscoverableAutoScanner("AA:00:00:00:1C:03", BluetoothScanningMode.AUTO)
    c_owner = manager.async_register_scanner(owner)
    c_active = manager.async_register_scanner(active)
    c_auto = manager.async_register_scanner(auto_fb)
    try:
        for addr in (addr_covered, addr_flipped, addr_orphan):
            _inject_with_rssi(owner, addr, rssi=-50)
        active.add_discovered(addr_covered, rssi=-60)
        auto_fb.add_discovered(addr_flipped, rssi=-60)
        owner._add_connecting(addr_covered)
        for addr in (addr_covered, addr_flipped, addr_orphan):
            _make_due(sched, addr)
        with caplog.at_level(logging.WARNING, logger="habluetooth.auto_scheduler"):
            await _run_worker_tick(sched, owner.source)
        assert owner.active_window_calls == []
        assert active.active_window_calls == []
        assert auto_fb.active_window_calls == [6.0]
        warnings_for_orphan = [
            record
            for record in caplog.records
            if "no fallback scanner" in record.message and addr_orphan in record.message
        ]
        assert len(warnings_for_orphan) == 1
        # Covered/flipped addresses must not appear in any no-fallback warning.
        assert not any(
            "no fallback scanner" in record.message and addr_covered in record.message
            for record in caplog.records
        )
        assert not any(
            "no fallback scanner" in record.message and addr_flipped in record.message
            for record in caplog.records
        )
    finally:
        owner._finished_connecting(addr_covered, connected=False)
        c1()
        c2()
        c3()
        c_owner()
        c_active()
        c_auto()


@pytest.mark.asyncio
async def test_worker_tick_owner_connecting_different_address_still_delegates() -> None:
    """
    The connecting-phase signal is per-scanner, not per-address.

    The owner is in a connect attempt to address X, while the due
    per-device window is for address Y. The owner's radio is still
    busy with X's connect, so Y must be delegated to a fallback too.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    addr_due = "11:22:33:44:55:1F"
    addr_connecting = "AA:BB:CC:DD:EE:99"
    cancel = manager.async_register_active_scan(
        addr_due, scan_interval=120.0, scan_duration=6.0
    )
    owner = _DiscoverableAutoScanner("AA:00:00:00:1F:01", BluetoothScanningMode.AUTO)
    fb = _DiscoverableAutoScanner("AA:00:00:00:1F:02", BluetoothScanningMode.AUTO)
    c_owner = manager.async_register_scanner(owner)
    c_fb = manager.async_register_scanner(fb)
    try:
        _inject_with_rssi(owner, addr_due, rssi=-50)
        fb.add_discovered(addr_due, rssi=-60)
        # Connect-in-progress is for a different address.
        owner._add_connecting(addr_connecting)
        _make_due(sched, addr_due)
        await _run_worker_tick(sched, owner.source)
        assert owner.active_window_calls == []
        assert fb.active_window_calls == [6.0]
    finally:
        owner._finished_connecting(addr_connecting, connected=False)
        cancel()
        c_owner()
        c_fb()


@pytest.mark.asyncio
async def test_worker_tick_fallback_returning_false_does_not_warn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    A fallback returning False from async_request_active_window is silent.

    The helper's contract is "True = window armed/extended,
    False = refused" — both are terminal answers. We consume the
    call without raising and without warning, consistent with the
    non-connecting path that also ignores the return value.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    address = "11:22:33:44:55:20"
    cancel = manager.async_register_active_scan(
        address, scan_interval=120.0, scan_duration=6.0
    )
    owner = _DiscoverableAutoScanner("AA:00:00:00:20:01", BluetoothScanningMode.AUTO)
    fb = _DiscoverableAutoScanner("AA:00:00:00:20:02", BluetoothScanningMode.AUTO)
    fb._return_value = False
    c_owner = manager.async_register_scanner(owner)
    c_fb = manager.async_register_scanner(fb)
    try:
        _inject_with_rssi(owner, address, rssi=-50)
        fb.add_discovered(address, rssi=-60)
        owner._add_connecting(address)
        _make_due(sched, address)
        with caplog.at_level(logging.WARNING, logger="habluetooth.auto_scheduler"):
            await _run_worker_tick(sched, owner.source)
        # Call was made, no warning, no exception escaped.
        assert fb.active_window_calls == [6.0]
        assert owner.active_window_calls == []
        assert not any(
            "no fallback scanner" in record.message for record in caplog.records
        )
    finally:
        owner._finished_connecting(address, connected=False)
        cancel()
        c_owner()
        c_fb()


@pytest.mark.asyncio
async def test_worker_tick_non_connectable_auto_fallback_is_eligible() -> None:
    """
    A non-connectable AUTO scanner is a valid fallback for scanning.

    Fallback selection is about *scanning*, not connecting — a
    non-connectable scanner that can see the device is just as good
    for an active-window flip as a connectable one.
    ``async_scanner_devices_by_address(address, False)`` is called
    with ``connectable=False`` so both lists are considered.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    address = "11:22:33:44:55:21"
    cancel = manager.async_register_active_scan(
        address, scan_interval=120.0, scan_duration=6.0
    )
    owner = _DiscoverableAutoScanner("AA:00:00:00:21:01", BluetoothScanningMode.AUTO)
    fb = _DiscoverableAutoScanner(
        "AA:00:00:00:21:02", BluetoothScanningMode.AUTO, connectable=False
    )
    c_owner = manager.async_register_scanner(owner)
    c_fb = manager.async_register_scanner(fb)
    try:
        _inject_with_rssi(owner, address, rssi=-50)
        fb.add_discovered(address, rssi=-60)
        owner._add_connecting(address)
        _make_due(sched, address)
        await _run_worker_tick(sched, owner.source)
        assert owner.active_window_calls == []
        assert fb.active_window_calls == [6.0]
    finally:
        owner._finished_connecting(address, connected=False)
        cancel()
        c_owner()
        c_fb()


@pytest.mark.asyncio
async def test_worker_tick_connect_starting_after_check_still_runs_locally() -> None:
    """
    Race: a connect that starts AFTER the connecting check still hits the owner.

    The connecting-state snapshot is taken once at the top of
    ``_tick``. If a connect begins between that check and the
    ``async_request_active_window`` await on the owner, the call has
    already been committed — we do not re-check mid-dispatch. The
    test pins this contract: ``_add_connecting`` after the call has
    started must not flip dispatch to a fallback for THIS tick.
    The next tick will see the new connecting state.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    address = "11:22:33:44:55:22"
    cancel = manager.async_register_active_scan(
        address, scan_interval=120.0, scan_duration=6.0
    )
    owner = _DiscoverableAutoScanner("AA:00:00:00:22:01", BluetoothScanningMode.AUTO)
    fb = _DiscoverableAutoScanner("AA:00:00:00:22:02", BluetoothScanningMode.AUTO)
    owner._block_event = asyncio.Event()
    c_owner = manager.async_register_scanner(owner)
    c_fb = manager.async_register_scanner(fb)
    try:
        _inject_with_rssi(owner, address, rssi=-50)
        fb.add_discovered(address, rssi=-60)
        # Owner NOT connecting at tick start.
        _make_due(sched, address)
        tick_task = asyncio.create_task(_run_worker_tick(sched, owner.source))
        # Yield so the worker enters its await on owner.async_request_active_window
        # (which is blocked on owner._block_event).
        await asyncio.sleep(0)
        assert owner.active_window_calls == [6.0]
        # Race window: connect starts after the check, while the
        # owner call is in flight. The mid-flight call must not be
        # diverted; the fallback must not be called for THIS tick.
        owner._add_connecting(address)
        owner._block_event.set()
        await tick_task
        assert fb.active_window_calls == []
    finally:
        owner._finished_connecting(address, connected=False)
        cancel()
        c_owner()
        c_fb()


@pytest.mark.asyncio
async def test_worker_tick_connect_finish_during_dispatch_keeps_dispatch() -> None:
    """
    Race: owner finishes connecting WHILE the fallback dispatch is awaiting.

    The connecting state was True at tick start, so we entered the
    fallback branch and already advanced ``_needs``. The connect
    completing mid-await must not cancel the in-flight fallback call
    nor cause a duplicate window on the owner.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    address = "11:22:33:44:55:23"
    cancel = manager.async_register_active_scan(
        address, scan_interval=120.0, scan_duration=6.0
    )
    owner = _DiscoverableAutoScanner("AA:00:00:00:23:01", BluetoothScanningMode.AUTO)
    fb = _DiscoverableAutoScanner("AA:00:00:00:23:02", BluetoothScanningMode.AUTO)
    fb._block_event = asyncio.Event()
    c_owner = manager.async_register_scanner(owner)
    c_fb = manager.async_register_scanner(fb)
    try:
        _inject_with_rssi(owner, address, rssi=-50)
        fb.add_discovered(address, rssi=-60)
        owner._add_connecting(address)
        _make_due(sched, address)
        tick_task = asyncio.create_task(_run_worker_tick(sched, owner.source))
        # Yield so the worker enters its await on the blocked fallback.
        await asyncio.sleep(0)
        assert fb.active_window_calls == [6.0]
        # Connect finishes mid-dispatch.
        owner._finished_connecting(address, connected=True)
        assert owner._connections_in_progress() == 0
        # Unblock the fallback so the dispatch can complete.
        fb._block_event.set()
        await tick_task
        # Dispatch completed on the fallback only; owner stayed
        # untouched for this tick.
        assert fb.active_window_calls == [6.0]
        assert owner.active_window_calls == []
    finally:
        cancel()
        c_owner()
        c_fb()


@pytest.mark.asyncio
async def test_worker_tick_two_owners_delegate_to_same_fallback_concurrently() -> None:
    """
    Cross-tick: two owner workers concurrently delegate to one fallback.

    The auto_scheduler doesn't serialize across workers — both
    deliveries go through. The scanner-level
    ``_active_window_handle`` / ``_start_stop_lock`` extend-if-extends
    logic is what guarantees the radio doesn't double-flip. Here we
    verify the auto_scheduler delivers both calls cleanly without
    deadlock or exception.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    addr_a = "11:22:33:44:55:24"
    addr_b = "11:22:33:44:55:25"
    c1 = manager.async_register_active_scan(
        addr_a, scan_interval=120.0, scan_duration=6.0
    )
    c2 = manager.async_register_active_scan(
        addr_b, scan_interval=120.0, scan_duration=8.0
    )
    owner_a = _DiscoverableAutoScanner("AA:00:00:00:24:01", BluetoothScanningMode.AUTO)
    owner_b = _DiscoverableAutoScanner("AA:00:00:00:24:02", BluetoothScanningMode.AUTO)
    fb = _DiscoverableAutoScanner("AA:00:00:00:24:03", BluetoothScanningMode.AUTO)
    c_a = manager.async_register_scanner(owner_a)
    c_b = manager.async_register_scanner(owner_b)
    c_fb = manager.async_register_scanner(fb)
    try:
        _inject_with_rssi(owner_a, addr_a, rssi=-50)
        _inject_with_rssi(owner_b, addr_b, rssi=-50)
        fb.add_discovered(addr_a, rssi=-60)
        fb.add_discovered(addr_b, rssi=-60)
        owner_a._add_connecting(addr_a)
        owner_b._add_connecting(addr_b)
        _make_due(sched, addr_a)
        _make_due(sched, addr_b)
        await asyncio.gather(
            _run_worker_tick(sched, owner_a.source),
            _run_worker_tick(sched, owner_b.source),
        )
        # Both owners delegated to fb; both calls were delivered.
        assert sorted(fb.active_window_calls) == [6.0, 8.0]
        assert owner_a.active_window_calls == []
        assert owner_b.active_window_calls == []
    finally:
        owner_a._finished_connecting(addr_a, connected=False)
        owner_b._finished_connecting(addr_b, connected=False)
        c1()
        c2()
        c_a()
        c_b()
        c_fb()


@pytest.mark.asyncio
async def test_worker_tick_ownership_flip_during_dispatch_no_double_fire() -> None:
    """
    Race: ownership flips from owner to fallback during the dispatch.

    Same protection as the non-connecting migration test: the
    pre-await ``_advance_due`` updates ``_needs`` to ``now +
    scan_interval`` before the fallback await, so if RSSI causes
    ownership to shift to the fallback mid-dispatch, the fallback's
    own next tick sees a future due time and skips — no duplicate
    window fires on the new owner.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    address = "11:22:33:44:55:26"
    cancel = manager.async_register_active_scan(
        address, scan_interval=90.0, scan_duration=6.0
    )
    owner = _DiscoverableAutoScanner("AA:00:00:00:26:01", BluetoothScanningMode.AUTO)
    fb = _DiscoverableAutoScanner("AA:00:00:00:26:02", BluetoothScanningMode.AUTO)
    fb._block_event = asyncio.Event()
    c_owner = manager.async_register_scanner(owner)
    c_fb = manager.async_register_scanner(fb)
    try:
        _inject_with_rssi(owner, address, rssi=-80)
        fb.add_discovered(address, rssi=-60)
        owner._add_connecting(address)
        _make_due(sched, address)
        before = loop.time()
        tick_task = asyncio.create_task(_run_worker_tick(sched, owner.source))
        await asyncio.sleep(0)
        # Confirm fallback call is in flight and entries advanced.
        assert fb.active_window_calls == [6.0]
        entries = sched._needs[address]
        for due in entries.values():
            assert due == pytest.approx(before + 90.0, abs=0.5)
        # Ownership flips to fb mid-dispatch (much stronger RSSI).
        _inject_with_rssi(fb, address, rssi=-30)
        info = manager.async_last_service_info(address, False)
        assert info is not None
        assert info.source == fb.source
        # fb's own next tick must skip because entries are already advanced.
        fb._block_event.set()
        await tick_task
        await sched._workers[fb.source]._tick()
        # Only one call landed on fb; no double-fire.
        assert fb.active_window_calls == [6.0]
        assert owner.active_window_calls == []
    finally:
        owner._finished_connecting(address, connected=False)
        cancel()
        c_owner()
        c_fb()


@pytest.mark.asyncio
async def test_worker_tick_resolver_excludes_owner_when_owner_self_reports() -> None:
    """
    The owner's own source is skipped even if it appears in the scanner list.

    If the owner's ``get_discovered_device_advertisement_data`` returns
    non-None for the address (so the manager lists it among the
    scanner-devices), the resolver must still skip it via the
    ``scanner.source == exclude_source`` guard rather than picking the
    busy owner as its own fallback.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    address = "11:22:33:44:55:27"
    cancel = manager.async_register_active_scan(
        address, scan_interval=120.0, scan_duration=6.0
    )
    owner = _DiscoverableAutoScanner("AA:00:00:00:27:01", BluetoothScanningMode.AUTO)
    fb = _DiscoverableAutoScanner("AA:00:00:00:27:02", BluetoothScanningMode.AUTO)
    c_owner = manager.async_register_scanner(owner)
    c_fb = manager.async_register_scanner(fb)
    try:
        _inject_with_rssi(owner, address, rssi=-50)
        # Owner self-reports: would normally be sorted as the highest-RSSI
        # candidate, but the resolver must exclude itself.
        owner.add_discovered(address, rssi=-30)
        fb.add_discovered(address, rssi=-70)
        owner._add_connecting(address)
        _make_due(sched, address)
        await _run_worker_tick(sched, owner.source)
        # Owner skipped despite self-reporting; weaker fallback wins.
        assert owner.active_window_calls == []
        assert fb.active_window_calls == [6.0]
    finally:
        owner._finished_connecting(address, connected=False)
        cancel()
        c_owner()
        c_fb()


@pytest.mark.asyncio
async def test_worker_tick_dispatch_advances_fallback_sweep_clock() -> None:
    """
    Delegating an active window to a fallback advances its sweep clock.

    The fallback's radio is actively scanning for ``duration`` seconds,
    which subsumes the work its own rediscovery sweep would do. We
    bump ``_sweep_last_completed = now`` so the fallback doesn't
    immediately schedule another sweep window on top of the one we
    just triggered.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    address = "11:22:33:44:55:28"
    cancel = manager.async_register_active_scan(
        address, scan_interval=120.0, scan_duration=6.0
    )
    owner = _DiscoverableAutoScanner("AA:00:00:00:28:01", BluetoothScanningMode.AUTO)
    fb = _DiscoverableAutoScanner("AA:00:00:00:28:02", BluetoothScanningMode.AUTO)
    c_owner = manager.async_register_scanner(owner)
    c_fb = manager.async_register_scanner(fb)
    try:
        _inject_with_rssi(owner, address, rssi=-50)
        fb.add_discovered(address, rssi=-60)
        # Make fb's own sweep imminent so we can detect the advance.
        fb_worker = sched._workers[fb.source]
        fb_worker._sweep_last_completed = loop.time() - AUTO_REDISCOVERY_INTERVAL - 1.0
        sweep_before = fb_worker._sweep_last_completed
        owner._add_connecting(address)
        _make_due(sched, address)
        before = loop.time()
        await _run_worker_tick(sched, owner.source)
        assert fb.active_window_calls == [6.0]
        # Sweep clock advanced to ~now so fb won't immediately resweep.
        assert fb_worker._sweep_last_completed > sweep_before
        assert fb_worker._sweep_last_completed >= before
    finally:
        owner._finished_connecting(address, connected=False)
        cancel()
        c_owner()
        c_fb()


@pytest.mark.asyncio
async def test_worker_tick_dispatch_sets_fallback_window_end() -> None:
    """
    Delegation marks the fallback worker as in-window for ``duration``.

    With ``fb._window_end > now``, the fallback's own ``_tick`` and
    ``_next_event_at`` short-circuit during the delegated window so
    the fallback doesn't redundantly tick on its own due work for the
    duration of the active scan it is already running.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    address = "11:22:33:44:55:29"
    cancel = manager.async_register_active_scan(
        address, scan_interval=120.0, scan_duration=6.0
    )
    owner = _DiscoverableAutoScanner("AA:00:00:00:29:01", BluetoothScanningMode.AUTO)
    fb = _DiscoverableAutoScanner("AA:00:00:00:29:02", BluetoothScanningMode.AUTO)
    c_owner = manager.async_register_scanner(owner)
    c_fb = manager.async_register_scanner(fb)
    try:
        _inject_with_rssi(owner, address, rssi=-50)
        fb.add_discovered(address, rssi=-60)
        owner._add_connecting(address)
        _make_due(sched, address)
        before = loop.time()
        await _run_worker_tick(sched, owner.source)
        fb_worker = sched._workers[fb.source]
        # Window-end bumped roughly to before + duration.
        assert fb_worker._window_end >= before + 5.0
        assert fb_worker._window_end <= loop.time() + 7.0
        # A fb tick while _window_end > now must short-circuit
        # (no async_request_active_window call recorded).
        calls_before = list(fb.active_window_calls)
        await fb_worker._tick()
        assert fb.active_window_calls == calls_before
    finally:
        owner._finished_connecting(address, connected=False)
        cancel()
        c_owner()
        c_fb()


@pytest.mark.asyncio
async def test_worker_tick_dispatch_does_not_shrink_existing_fallback_window() -> None:
    """
    A larger pre-existing ``_window_end`` on the fallback is preserved.

    If the fallback is already running a longer window when we
    delegate (e.g., a much earlier delegation from another owner
    extended its own ``_window_end``), our shorter delegation must
    not shrink it back.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    address = "11:22:33:44:55:2A"
    cancel = manager.async_register_active_scan(
        address, scan_interval=120.0, scan_duration=5.0
    )
    owner = _DiscoverableAutoScanner("AA:00:00:00:2A:01", BluetoothScanningMode.AUTO)
    fb = _DiscoverableAutoScanner("AA:00:00:00:2A:02", BluetoothScanningMode.AUTO)
    c_owner = manager.async_register_scanner(owner)
    c_fb = manager.async_register_scanner(fb)
    try:
        _inject_with_rssi(owner, address, rssi=-50)
        fb.add_discovered(address, rssi=-60)
        fb_worker = sched._workers[fb.source]
        # Pre-seed a longer pending window on the fallback.
        existing_window_end = loop.time() + 60.0
        fb_worker._window_end = existing_window_end
        owner._add_connecting(address)
        _make_due(sched, address)
        await _run_worker_tick(sched, owner.source)
        # The shorter (5s) delegation must not have shrunk the
        # existing 60s window.
        assert fb_worker._window_end == existing_window_end
    finally:
        owner._finished_connecting(address, connected=False)
        cancel()
        c_owner()
        c_fb()


@pytest.mark.asyncio
async def test_worker_tick_three_addresses_same_fallback_coalesce_to_max() -> None:
    """
    Three due addresses with distinct durations on one fallback coalesce to max.

    Confirms per-fallback coalescing picks the max
    ``scan_duration`` over all grouped requests, not the first.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    addr_a = "11:22:33:44:55:2B"
    addr_b = "11:22:33:44:55:2C"
    addr_c = "11:22:33:44:55:2D"
    c1 = manager.async_register_active_scan(
        addr_a, scan_interval=120.0, scan_duration=6.0
    )
    c2 = manager.async_register_active_scan(
        addr_b, scan_interval=120.0, scan_duration=11.0
    )
    c3 = manager.async_register_active_scan(
        addr_c, scan_interval=120.0, scan_duration=8.0
    )
    owner = _DiscoverableAutoScanner("AA:00:00:00:2B:01", BluetoothScanningMode.AUTO)
    fb = _DiscoverableAutoScanner("AA:00:00:00:2B:02", BluetoothScanningMode.AUTO)
    c_owner = manager.async_register_scanner(owner)
    c_fb = manager.async_register_scanner(fb)
    try:
        for addr in (addr_a, addr_b, addr_c):
            _inject_with_rssi(owner, addr, rssi=-50)
            fb.add_discovered(addr, rssi=-60)
        owner._add_connecting(addr_a)
        for addr in (addr_a, addr_b, addr_c):
            _make_due(sched, addr)
        await _run_worker_tick(sched, owner.source)
        # Single call to the shared fallback at max(6, 11, 8) = 11.
        assert fb.active_window_calls == [11.0]
        assert owner.active_window_calls == []
    finally:
        owner._finished_connecting(addr_a, connected=False)
        c1()
        c2()
        c3()
        c_owner()
        c_fb()


@pytest.mark.asyncio
async def test_worker_tick_three_addresses_three_fallbacks_each_own_duration() -> None:
    """Three due addresses on three fallbacks, each call uses its own duration."""
    manager = get_manager()
    sched = manager._auto_scheduler
    addr_a = "11:22:33:44:55:2E"
    addr_b = "11:22:33:44:55:2F"
    addr_c = "11:22:33:44:55:30"
    c1 = manager.async_register_active_scan(
        addr_a, scan_interval=120.0, scan_duration=6.0
    )
    c2 = manager.async_register_active_scan(
        addr_b, scan_interval=180.0, scan_duration=9.0
    )
    c3 = manager.async_register_active_scan(
        addr_c, scan_interval=240.0, scan_duration=12.0
    )
    owner = _DiscoverableAutoScanner("AA:00:00:00:2E:01", BluetoothScanningMode.AUTO)
    fb_a = _DiscoverableAutoScanner("AA:00:00:00:2E:02", BluetoothScanningMode.AUTO)
    fb_b = _DiscoverableAutoScanner("AA:00:00:00:2E:03", BluetoothScanningMode.AUTO)
    fb_c = _DiscoverableAutoScanner("AA:00:00:00:2E:04", BluetoothScanningMode.AUTO)
    c_owner = manager.async_register_scanner(owner)
    c_a = manager.async_register_scanner(fb_a)
    c_b = manager.async_register_scanner(fb_b)
    c_c = manager.async_register_scanner(fb_c)
    try:
        _inject_with_rssi(owner, addr_a, rssi=-50)
        _inject_with_rssi(owner, addr_b, rssi=-50)
        _inject_with_rssi(owner, addr_c, rssi=-50)
        fb_a.add_discovered(addr_a, rssi=-60)
        fb_b.add_discovered(addr_b, rssi=-60)
        fb_c.add_discovered(addr_c, rssi=-60)
        owner._add_connecting(addr_a)
        for addr in (addr_a, addr_b, addr_c):
            _make_due(sched, addr)
        await _run_worker_tick(sched, owner.source)
        assert fb_a.active_window_calls == [6.0]
        assert fb_b.active_window_calls == [9.0]
        assert fb_c.active_window_calls == [12.0]
        assert owner.active_window_calls == []
    finally:
        owner._finished_connecting(addr_a, connected=False)
        c1()
        c2()
        c3()
        c_owner()
        c_a()
        c_b()
        c_c()


@pytest.mark.asyncio
async def test_worker_tick_three_addresses_no_fallback_advance_by_defer() -> None:
    """
    No-fallback advance uses ``_AUTO_CONNECTING_DEFER``, not scan_interval.

    Three addresses with very different ``scan_interval``s
    (120/600/3600s) must all be advanced to ~now + 30s so the next
    tick retries shortly after the connect completes.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    addr_a = "11:22:33:44:55:31"
    addr_b = "11:22:33:44:55:32"
    addr_c = "11:22:33:44:55:33"
    c1 = manager.async_register_active_scan(
        addr_a, scan_interval=120.0, scan_duration=6.0
    )
    c2 = manager.async_register_active_scan(
        addr_b, scan_interval=600.0, scan_duration=9.0
    )
    c3 = manager.async_register_active_scan(
        addr_c, scan_interval=3600.0, scan_duration=12.0
    )
    owner = _DiscoverableAutoScanner("AA:00:00:00:31:01", BluetoothScanningMode.AUTO)
    c_owner = manager.async_register_scanner(owner)
    try:
        for addr in (addr_a, addr_b, addr_c):
            _inject_with_rssi(owner, addr, rssi=-50)
            _make_due(sched, addr)
        owner._add_connecting(addr_a)
        before = loop.time()
        await _run_worker_tick(sched, owner.source)
        for addr in (addr_a, addr_b, addr_c):
            entries = sched._needs[addr]
            for due in entries.values():
                assert due == pytest.approx(before + 30.0, abs=0.5)
                assert due < before + 60.0
    finally:
        owner._finished_connecting(addr_a, connected=False)
        c1()
        c2()
        c3()
        c_owner()


@pytest.mark.asyncio
async def test_note_window_dispatched_preserves_more_recent_sweep() -> None:
    """
    ``note_window_dispatched`` does not move ``_sweep_last_completed`` backwards.

    Covers the False branch of ``if self._sweep_last_completed < now``
    when the fallback's sweep clock is already further in the future
    than the ``now`` we're passing in.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    address = "11:22:33:44:55:38"
    cancel = manager.async_register_active_scan(
        address, scan_interval=120.0, scan_duration=6.0
    )
    owner = _DiscoverableAutoScanner("AA:00:00:00:38:01", BluetoothScanningMode.AUTO)
    fb = _DiscoverableAutoScanner("AA:00:00:00:38:02", BluetoothScanningMode.AUTO)
    c_owner = manager.async_register_scanner(owner)
    c_fb = manager.async_register_scanner(fb)
    try:
        _inject_with_rssi(owner, address, rssi=-50)
        fb.add_discovered(address, rssi=-60)
        fb_worker = sched._workers[fb.source]
        future_sweep = loop.time() + 600.0
        fb_worker._sweep_last_completed = future_sweep
        owner._add_connecting(address)
        _make_due(sched, address)
        await _run_worker_tick(sched, owner.source)
        # Sweep clock not moved backwards by our note_window_dispatched.
        assert fb_worker._sweep_last_completed == future_sweep
        assert fb.active_window_calls == [6.0]
    finally:
        owner._finished_connecting(address, connected=False)
        cancel()
        c_owner()
        c_fb()


@pytest.mark.asyncio
async def test_worker_tick_dispatch_handles_missing_fallback_worker() -> None:
    """
    Dispatch tolerates ``workers.get(fb.source) is None``.

    Covers the False branch of ``if fb_worker is not None``. Reachable
    when a fallback scanner is unregistered between resolution and the
    per-fallback iteration (sim: drop the worker entry between
    registration and tick to force the lookup miss). The dispatch
    should still call ``async_request_active_window`` on the fallback
    even with no worker available to receive ``note_window_dispatched``.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    address = "11:22:33:44:55:37"
    cancel = manager.async_register_active_scan(
        address, scan_interval=120.0, scan_duration=6.0
    )
    owner = _DiscoverableAutoScanner("AA:00:00:00:37:01", BluetoothScanningMode.AUTO)
    fb = _DiscoverableAutoScanner("AA:00:00:00:37:02", BluetoothScanningMode.AUTO)
    c_owner = manager.async_register_scanner(owner)
    c_fb = manager.async_register_scanner(fb)
    try:
        _inject_with_rssi(owner, address, rssi=-50)
        fb.add_discovered(address, rssi=-60)
        owner._add_connecting(address)
        _make_due(sched, address)
        # Drop fb's worker so workers.get(fb.source) is None during dispatch.
        # The fb scanner is still registered (so resolve still picks it)
        # and async_scanner_devices_by_address still returns it.
        sched._workers.pop(fb.source)
        await _run_worker_tick(sched, owner.source)
        # Dispatch still happened on the fallback's scanner even though
        # we couldn't notify the worker.
        assert fb.active_window_calls == [6.0]
        assert owner.active_window_calls == []
    finally:
        owner._finished_connecting(address, connected=False)
        cancel()
        c_owner()
        c_fb()


@pytest.mark.asyncio
async def test_worker_tick_three_addresses_mixed_outcomes_advance_correctly() -> None:
    """
    Three addresses, three outcomes, each advanced per its outcome.

    covered (ACTIVE) -> ``scan_interval`` (full cadence).
    AUTO fallback   -> ``scan_interval`` (full cadence).
    no fallback     -> ``_AUTO_CONNECTING_DEFER`` (short retry).
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    addr_covered = "11:22:33:44:55:34"
    addr_flipped = "11:22:33:44:55:35"
    addr_orphan = "11:22:33:44:55:36"
    c1 = manager.async_register_active_scan(
        addr_covered, scan_interval=120.0, scan_duration=6.0
    )
    c2 = manager.async_register_active_scan(
        addr_flipped, scan_interval=240.0, scan_duration=9.0
    )
    c3 = manager.async_register_active_scan(
        addr_orphan, scan_interval=600.0, scan_duration=12.0
    )
    owner = _DiscoverableAutoScanner("AA:00:00:00:34:01", BluetoothScanningMode.AUTO)
    active = _DiscoverableAutoScanner("AA:00:00:00:34:02", BluetoothScanningMode.ACTIVE)
    fb = _DiscoverableAutoScanner("AA:00:00:00:34:03", BluetoothScanningMode.AUTO)
    c_owner = manager.async_register_scanner(owner)
    c_active = manager.async_register_scanner(active)
    c_fb = manager.async_register_scanner(fb)
    try:
        for addr in (addr_covered, addr_flipped, addr_orphan):
            _inject_with_rssi(owner, addr, rssi=-50)
            _make_due(sched, addr)
        active.add_discovered(addr_covered, rssi=-60)
        fb.add_discovered(addr_flipped, rssi=-60)
        owner._add_connecting(addr_covered)
        before = loop.time()
        await _run_worker_tick(sched, owner.source)
        for due in sched._needs[addr_covered].values():
            assert due == pytest.approx(before + 120.0, abs=0.5)
        for due in sched._needs[addr_flipped].values():
            assert due == pytest.approx(before + 240.0, abs=0.5)
        for due in sched._needs[addr_orphan].values():
            assert due == pytest.approx(before + 30.0, abs=0.5)
        assert fb.active_window_calls == [9.0]
        assert active.active_window_calls == []
        assert owner.active_window_calls == []
    finally:
        owner._finished_connecting(addr_covered, connected=False)
        c1()
        c2()
        c3()
        c_owner()
        c_active()
        c_fb()


@pytest.mark.asyncio
async def test_worker_tick_fallback_with_none_rssi_is_still_dispatched() -> None:
    """
    A fallback whose last advertisement has ``rssi is None`` is usable.

    Pins Kōan blocker #1: ``_resolve_fallback_for_address`` must not
    crash on ``None`` RSSI (would raise ``TypeError`` on ``None >
    -10_000``). The defensive ``rssi or NO_RSSI_VALUE`` normalization
    keeps the scanner in the candidate pool with the sentinel score.
    Here the single fallback has ``rssi=None`` and the dispatch must
    still fire on it.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    address = "11:22:33:44:55:39"
    cancel = manager.async_register_active_scan(
        address, scan_interval=120.0, scan_duration=6.0
    )
    owner = _DiscoverableAutoScanner("AA:00:00:00:39:01", BluetoothScanningMode.AUTO)
    fb = _DiscoverableAutoScanner("AA:00:00:00:39:02", BluetoothScanningMode.AUTO)
    c_owner = manager.async_register_scanner(owner)
    c_fb = manager.async_register_scanner(fb)
    try:
        _inject_with_rssi(owner, address, rssi=-50)
        fb.add_discovered(address, rssi=None)
        owner._add_connecting(address)
        _make_due(sched, address)
        await _run_worker_tick(sched, owner.source)
        assert owner.active_window_calls == []
        assert fb.active_window_calls == [6.0]
    finally:
        owner._finished_connecting(address, connected=False)
        cancel()
        c_owner()
        c_fb()


@pytest.mark.asyncio
async def test_worker_tick_fallback_with_rssi_loses_to_better_fallback() -> None:
    """
    A ``None``-RSSI fallback loses to a fallback with a real RSSI.

    With ``None`` normalized to ``NO_RSSI_VALUE`` (-127), any
    real-world RSSI beats it, so the ``None``-RSSI scanner is only
    picked when nothing else is available.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    address = "11:22:33:44:55:3A"
    cancel = manager.async_register_active_scan(
        address, scan_interval=120.0, scan_duration=6.0
    )
    owner = _DiscoverableAutoScanner("AA:00:00:00:3A:01", BluetoothScanningMode.AUTO)
    fb_none = _DiscoverableAutoScanner("AA:00:00:00:3A:02", BluetoothScanningMode.AUTO)
    fb_real = _DiscoverableAutoScanner("AA:00:00:00:3A:03", BluetoothScanningMode.AUTO)
    c_owner = manager.async_register_scanner(owner)
    c_n = manager.async_register_scanner(fb_none)
    c_r = manager.async_register_scanner(fb_real)
    try:
        _inject_with_rssi(owner, address, rssi=-50)
        fb_none.add_discovered(address, rssi=None)
        fb_real.add_discovered(address, rssi=-90)
        owner._add_connecting(address)
        _make_due(sched, address)
        await _run_worker_tick(sched, owner.source)
        # Real RSSI (-90) beats the normalized None (-127).
        assert fb_real.active_window_calls == [6.0]
        assert fb_none.active_window_calls == []
    finally:
        owner._finished_connecting(address, connected=False)
        cancel()
        c_owner()
        c_n()
        c_r()


@pytest.mark.asyncio
async def test_worker_tick_failed_fallback_advances_entries_by_full_interval() -> None:
    """
    Failed fallback dispatch still advances entries by ``scan_interval``.

    Pins Kōan suggestion #2 / the documented "advance on failure"
    semantics: when ``fb.async_request_active_window`` raises, the
    per-address entries have already been advanced by ``scan_interval``
    (NOT reset to ``retry_at``) and the fallback worker's
    ``_window_end`` / ``_sweep_last_completed`` bumps from
    ``note_window_dispatched`` are preserved. A failing fallback is
    treated like a successful one to avoid busy-looping on a stuck
    scanner.
    """

    class _RaisingScanner(_DiscoverableAutoScanner):
        async def async_request_active_window(self, duration: float) -> bool:
            self.active_window_calls.append(duration)
            msg = "boom"
            raise RuntimeError(msg)

    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    address = "11:22:33:44:55:3B"
    cancel = manager.async_register_active_scan(
        address, scan_interval=120.0, scan_duration=6.0
    )
    owner = _DiscoverableAutoScanner("AA:00:00:00:3B:01", BluetoothScanningMode.AUTO)
    fb = _RaisingScanner("AA:00:00:00:3B:02", BluetoothScanningMode.AUTO)
    c_owner = manager.async_register_scanner(owner)
    c_fb = manager.async_register_scanner(fb)
    try:
        _inject_with_rssi(owner, address, rssi=-50)
        fb.add_discovered(address, rssi=-60)
        owner._add_connecting(address)
        _make_due(sched, address)
        fb_worker = sched._workers[fb.source]
        sweep_before = fb_worker._sweep_last_completed
        before = loop.time()
        await _run_worker_tick(sched, owner.source)
        # Entries advanced by full scan_interval, NOT retry_at.
        for due in sched._needs[address].values():
            assert due == pytest.approx(before + 120.0, abs=0.5)
            assert due > before + 60.0  # well past the 30s retry_at
        # fb_worker bumps from note_window_dispatched are preserved.
        assert fb_worker._sweep_last_completed > sweep_before
        assert fb_worker._sweep_last_completed >= before
    finally:
        owner._finished_connecting(address, connected=False)
        cancel()
        c_owner()
        c_fb()


@pytest.mark.asyncio
async def test_worker_tick_fallback_with_rssi_zero_is_strongest() -> None:
    """
    A fallback with ``rssi == 0`` beats a fallback with negative RSSI.

    Pins the explicit ``rssi is None`` check (rather than ``rssi or
    NO_RSSI_VALUE``): an RSSI of 0 is a valid very-strong signal and
    must not be coerced to the missing-RSSI sentinel via ``0 or X``
    falsiness.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    address = "11:22:33:44:55:3D"
    cancel = manager.async_register_active_scan(
        address, scan_interval=120.0, scan_duration=6.0
    )
    owner = _DiscoverableAutoScanner("AA:00:00:00:3D:01", BluetoothScanningMode.AUTO)
    fb_zero = _DiscoverableAutoScanner("AA:00:00:00:3D:02", BluetoothScanningMode.AUTO)
    fb_neg = _DiscoverableAutoScanner("AA:00:00:00:3D:03", BluetoothScanningMode.AUTO)
    c_owner = manager.async_register_scanner(owner)
    c_zero = manager.async_register_scanner(fb_zero)
    c_neg = manager.async_register_scanner(fb_neg)
    try:
        _inject_with_rssi(owner, address, rssi=-50)
        fb_zero.add_discovered(address, rssi=0)
        fb_neg.add_discovered(address, rssi=-50)
        owner._add_connecting(address)
        _make_due(sched, address)
        await _run_worker_tick(sched, owner.source)
        # rssi=0 beats rssi=-50; the falsy-or pattern would have
        # incorrectly normalised 0 to NO_RSSI_VALUE and lost.
        assert fb_zero.active_window_calls == [6.0]
        assert fb_neg.active_window_calls == []
    finally:
        owner._finished_connecting(address, connected=False)
        cancel()
        c_owner()
        c_zero()
        c_neg()


@pytest.mark.asyncio
async def test_worker_tick_dispatch_short_window_still_resets_full_sweep() -> None:
    """
    A 5s delegated window resets the fallback's full 12h sweep cadence.

    Pins the documented best-effort caveat: ``note_window_dispatched``
    advances ``_sweep_last_completed`` to ``now`` regardless of how
    short the delegated window is. With min duration (5s, well below
    ``AUTO_REDISCOVERY_SWEEP_DURATION`` of 15s), the next sweep is
    still pushed out a full ``AUTO_REDISCOVERY_INTERVAL`` (12h).
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    address = "11:22:33:44:55:3C"
    # Request duration well under MIN; coalesce_duration clamps to
    # _AUTO_WINDOW_MIN_DURATION (5.0).
    cancel = manager.async_register_active_scan(
        address, scan_interval=120.0, scan_duration=5.0
    )
    owner = _DiscoverableAutoScanner("AA:00:00:00:3C:01", BluetoothScanningMode.AUTO)
    fb = _DiscoverableAutoScanner("AA:00:00:00:3C:02", BluetoothScanningMode.AUTO)
    c_owner = manager.async_register_scanner(owner)
    c_fb = manager.async_register_scanner(fb)
    try:
        _inject_with_rssi(owner, address, rssi=-50)
        fb.add_discovered(address, rssi=-60)
        # Place fb's sweep clock far in the past so we can see the bump.
        fb_worker = sched._workers[fb.source]
        fb_worker._sweep_last_completed = loop.time() - AUTO_REDISCOVERY_INTERVAL / 2
        owner._add_connecting(address)
        _make_due(sched, address)
        before = loop.time()
        await _run_worker_tick(sched, owner.source)
        # Delegated window was 5s; fb's next sweep was pushed to
        # roughly now + 12h regardless of the short window.
        next_sweep_due = fb_worker._sweep_last_completed + AUTO_REDISCOVERY_INTERVAL
        assert next_sweep_due == pytest.approx(
            before + AUTO_REDISCOVERY_INTERVAL, abs=1
        )
        assert fb.active_window_calls == [5.0]
    finally:
        owner._finished_connecting(address, connected=False)
        cancel()
        c_owner()
        c_fb()


@pytest.mark.asyncio
async def test_worker_tick_per_device_window_satisfies_sweep_floor() -> None:
    """
    A per-device active window advances ``_sweep_last_completed``.

    The rediscovery sweep is a floor: scanners that haven't
    active-scanned in 12 h get a 15 s sweep. A scanner that just ran
    a per-device active window has already actively scanned, so its
    next sweep is pushed out a full ``AUTO_REDISCOVERY_INTERVAL``
    even when ``sweep_due`` was False at tick time.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    address = "11:22:33:44:55:3E"
    cancel = manager.async_register_active_scan(
        address, scan_interval=120.0, scan_duration=6.0
    )
    scanner = _DiscoverableAutoScanner("AA:00:00:00:3E:01", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        _inject_with_rssi(scanner, address, rssi=-50)
        worker = sched._workers[scanner.source]
        # Place sweep clock recent enough that sweep is NOT due — we
        # want to prove the per-device window still advances it.
        recent_sweep = loop.time() - 60.0
        worker._sweep_last_completed = recent_sweep
        _make_due(sched, address)
        before = loop.time()
        await _run_worker_tick(sched, scanner.source)
        assert scanner.active_window_calls == [6.0]
        # Per-device window pushed sweep clock from "60s ago" to "now",
        # demonstrating any active scan satisfies the sweep floor.
        assert worker._sweep_last_completed > recent_sweep
        assert worker._sweep_last_completed >= before
    finally:
        cancel()
        register_cancel()


@pytest.mark.asyncio
async def test_worker_tick_dispatch_samples_time_per_fallback() -> None:
    """
    Each fallback's ``window_end`` is anchored to its dispatch time.

    Each ``await fb.async_request_active_window(duration)`` can take
    seconds in production (scanner stop/restart on Linux). Reusing
    the owner's tick-start ``now`` for every fallback's
    ``note_window_dispatched`` would leave later fallbacks'
    ``_window_end`` in the past — defeating the suppression. Use a
    first fallback that ``asyncio.sleep``s during its dispatch so the
    second fallback's ``loop.time()`` is strictly later than the
    owner's tick-start ``now``, then verify the second fallback's
    ``_window_end`` reflects its own dispatch time.
    """

    class _SlowScanner(_DiscoverableAutoScanner):
        async def async_request_active_window(self, duration: float) -> bool:
            self.active_window_calls.append(duration)
            await asyncio.sleep(0.1)
            return self._return_value

    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    addr_a = "11:22:33:44:55:3F"
    addr_b = "11:22:33:44:55:40"
    c1 = manager.async_register_active_scan(
        addr_a, scan_interval=120.0, scan_duration=6.0
    )
    c2 = manager.async_register_active_scan(
        addr_b, scan_interval=120.0, scan_duration=6.0
    )
    owner = _DiscoverableAutoScanner("AA:00:00:00:3F:01", BluetoothScanningMode.AUTO)
    fb_slow = _SlowScanner("AA:00:00:00:3F:02", BluetoothScanningMode.AUTO)
    fb_late = _DiscoverableAutoScanner("AA:00:00:00:3F:03", BluetoothScanningMode.AUTO)
    c_owner = manager.async_register_scanner(owner)
    c_s = manager.async_register_scanner(fb_slow)
    c_l = manager.async_register_scanner(fb_late)
    try:
        _inject_with_rssi(owner, addr_a, rssi=-50)
        _inject_with_rssi(owner, addr_b, rssi=-50)
        fb_slow.add_discovered(addr_a, rssi=-60)
        fb_late.add_discovered(addr_b, rssi=-60)
        owner._add_connecting(addr_a)
        _make_due(sched, addr_a)
        _make_due(sched, addr_b)
        tick_start = loop.time()
        await _run_worker_tick(sched, owner.source)
        # Both fallbacks were called.
        assert fb_slow.active_window_calls == [6.0]
        assert fb_late.active_window_calls == [6.0]
        # fb_late was dispatched AFTER fb_slow's 0.1s sleep, so its
        # _window_end is anchored to dispatch_now ≈ tick_start + 0.1,
        # i.e. > tick_start + duration (6.0). The owner's tick-start
        # ``now`` would have given tick_start + 6.0 ≈ tick_start + 6.0
        # exactly, which is < tick_start + 6.0 + 0.05.
        fb_late_worker = sched._workers[fb_late.source]
        assert fb_late_worker._window_end > tick_start + 6.0 + 0.05
    finally:
        owner._finished_connecting(addr_a, connected=False)
        c1()
        c2()
        c_owner()
        c_s()
        c_l()


@pytest.mark.asyncio
async def test_async_diagnostics() -> None:
    """Diagnostics expose per-worker sweep timing and per-address requests."""
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    address = "11:22:33:44:55:66"
    cancel1 = manager.async_register_active_scan(
        address, scan_interval=120.0, scan_duration=5.0
    )
    cancel2 = manager.async_register_active_scan(
        address, scan_interval=240.0, scan_duration=10.0
    )
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        _inject(scanner, address)
        diagnostics = sched.async_diagnostics()
        assert diagnostics["running"] is True
        assert diagnostics["monotonic_time"] == pytest.approx(loop.time(), abs=0.5)
        workers = diagnostics["workers"]
        assert set(workers) == {scanner.source}
        worker_diag = workers[scanner.source]
        assert worker_diag["name"] == scanner.name
        assert worker_diag["window_end"] == 0.0
        assert worker_diag["failed_window"] is False
        assert worker_diag["warned_no_fallback"] is False
        assert worker_diag["next_sweep_at"] == pytest.approx(
            worker_diag["sweep_last_completed"] + AUTO_REDISCOVERY_INTERVAL
        )
        assert worker_diag["next_event_at"] > 0.0
        requests = diagnostics["requests"]
        assert set(requests) == {address}
        entries = requests[address]
        assert len(entries) == 2
        pairs = sorted(
            (entry["scan_interval"], entry["scan_duration"]) for entry in entries
        )
        assert pairs == [(120.0, 5.0), (240.0, 10.0)]
        for entry in entries:
            assert entry["owner_source"] == scanner.source
            assert entry["next_due"] is not None
            assert entry["next_due"] > loop.time()
    finally:
        cancel1()
        cancel2()
        register_cancel()
    # After cancellation the address falls out of both indexes.
    post = sched.async_diagnostics()
    assert post["requests"] == {}


@contextlib.asynccontextmanager
async def _no_real_sleep():
    """
    Replace ``asyncio.sleep`` with an immediate fake-time advance.

    Sweeps clamp duration to AUTO_WINDOW_MIN_DURATION (5s); stubbing
    the sleep keeps tests fast while preserving the call shape so we
    can still observe what duration was requested. Each mocked sleep
    also advances ``loop.time()`` by ``duration`` so the on-demand
    sweep's sleep-until-end loop (which re-reads
    ``_on_demand_sweep_end`` on each wake) terminates instead of
    spinning forever against a frozen clock.
    """
    loop = asyncio.get_running_loop()
    real_time = loop.time
    fake_advance = [0.0]

    async def _instant(duration: float) -> None:
        fake_advance[0] += duration

    def _fake_time() -> float:
        return real_time() + fake_advance[0]

    with (
        patch("asyncio.sleep", new=_instant),
        patch.object(loop, "time", _fake_time),
    ):
        yield


@pytest.mark.asyncio
async def test_async_request_active_scan_fires_active_window_on_each_auto_scanner() -> (
    None
):
    """A sweep flips every AUTO scanner into ACTIVE for the duration."""
    manager = get_manager()
    a = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    b = _RecordingAutoScanner("AA:BB:CC:DD:EE:01", BluetoothScanningMode.AUTO)
    c_a = manager.async_register_scanner(a)
    c_b = manager.async_register_scanner(b)
    try:
        async with _no_real_sleep():
            await manager.async_request_active_scan(duration=7.0)
        assert a.active_window_calls == [7.0]
        assert b.active_window_calls == [7.0]
    finally:
        c_a()
        c_b()


@pytest.mark.asyncio
async def test_async_request_active_scan_skips_connecting_scanner() -> None:
    """A scanner mid-connect is skipped; non-connecting peers still flip."""
    manager = get_manager()
    busy = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    free = _RecordingAutoScanner("AA:BB:CC:DD:EE:01", BluetoothScanningMode.AUTO)
    c_busy = manager.async_register_scanner(busy)
    c_free = manager.async_register_scanner(free)
    busy._add_connecting("11:22:33:44:55:66")
    try:
        async with _no_real_sleep():
            await manager.async_request_active_scan(duration=5.0)
        assert busy.active_window_calls == []
        assert free.active_window_calls == [5.0]
    finally:
        busy._finished_connecting("11:22:33:44:55:66", connected=False)
        c_busy()
        c_free()


@pytest.mark.asyncio
async def test_async_request_active_scan_resets_next_sweep_time() -> None:
    """A sweep advances each flipped worker's _sweep_last_completed to now."""
    manager = get_manager()
    sched = manager._auto_scheduler
    loop = asyncio.get_running_loop()
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    worker = sched._workers[scanner.source]
    # Backdate so we can observe the bump.
    worker._sweep_last_completed = loop.time() - AUTO_REDISCOVERY_INTERVAL - 1.0
    try:
        before = loop.time()
        async with _no_real_sleep():
            await manager.async_request_active_scan(duration=5.0)
        assert worker._sweep_last_completed >= before
        assert worker._sweep_last_completed <= loop.time() + 0.1
    finally:
        register_cancel()


@pytest.mark.asyncio
async def test_async_request_active_scan_mixed_durations_extends_to_longest() -> None:
    """
    Concurrent callers asking for (10, 15, 5, 20) all wait until T0+20.

    The first caller to win the check-and-set runs a 10s sweep; the 15s
    and 20s callers extend the in-flight window (re-flipping the radio
    with the longer remaining duration); the 5s caller fits within the
    already-extended end and does not flip again. The scanner records
    each flip's duration so we can verify the extension chain.
    """
    manager = get_manager()
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        async with _no_real_sleep():
            await asyncio.gather(
                manager.async_request_active_scan(duration=10.0),
                manager.async_request_active_scan(duration=15.0),
                manager.async_request_active_scan(duration=5.0),
                manager.async_request_active_scan(duration=20.0),
            )
        # Exactly three flip durations: leader's 10s + two extensions
        # (approximately 15s and 20s, with sub-second drift from
        # task-start jitter — pytest.approx absorbs the drift, and
        # the ordering pins the chain.
        assert len(scanner.active_window_calls) == 3
        assert scanner.active_window_calls[0] == 10.0
        assert scanner.active_window_calls[1] == pytest.approx(15.0, abs=1.0)
        assert scanner.active_window_calls[2] == pytest.approx(20.0, abs=1.0)
        assert (
            scanner.active_window_calls[0]
            < scanner.active_window_calls[1]
            < scanner.active_window_calls[2]
        )
        # The future and end are cleared once the leader finishes.
        assert manager._auto_scheduler._on_demand_sweep_future is None
        assert manager._auto_scheduler._on_demand_sweep_end == 0.0
    finally:
        register_cancel()


@pytest.mark.asyncio
async def test_async_request_active_scan_dedupes_concurrent_callers() -> None:
    """N concurrent sweep calls share one window; the bus flips once."""
    manager = get_manager()
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        async with _no_real_sleep():
            # Three concurrent callers, mirroring HA integrations each
            # opening their own config flow at the same time.
            await asyncio.gather(
                manager.async_request_active_scan(duration=5.0),
                manager.async_request_active_scan(duration=5.0),
                manager.async_request_active_scan(duration=5.0),
            )
        # Only one active window despite three callers.
        assert scanner.active_window_calls == [5.0]
        # The deduped future is cleared once the sweep finishes.
        assert manager._auto_scheduler._on_demand_sweep_future is None
    finally:
        register_cancel()


@pytest.mark.asyncio
async def test_async_request_active_scan_default_duration_is_10s() -> None:
    """Calling without a duration uses DEFAULT_ON_DEMAND_SWEEP_DURATION (10s)."""
    manager = get_manager()
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        async with _no_real_sleep():
            await manager.async_request_active_scan()
        assert scanner.active_window_calls == [DEFAULT_ON_DEMAND_SWEEP_DURATION]
    finally:
        register_cancel()


@pytest.mark.asyncio
async def test_async_request_active_scan_clamps_to_window_bounds() -> None:
    """Out-of-range durations are clamped to [MIN, MAX]."""
    manager = get_manager()
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        async with _no_real_sleep():
            await manager.async_request_active_scan(duration=0.5)  # below MIN
            await manager.async_request_active_scan(duration=999.0)  # above MAX
        assert scanner.active_window_calls == [
            AUTO_WINDOW_MIN_DURATION,
            AUTO_WINDOW_MAX_DURATION,
        ]
    finally:
        register_cancel()


@pytest.mark.asyncio
async def test_async_request_active_scan_rejects_invalid_duration() -> None:
    """NaN, inf, zero, and negative durations raise ValueError."""
    manager = get_manager()
    for bad in (float("nan"), float("inf"), float("-inf"), 0.0, -1.0):
        with pytest.raises(ValueError, match="finite positive"):
            await manager.async_request_active_scan(duration=bad)


@pytest.mark.asyncio
async def test_async_request_active_scan_no_op_when_scheduler_stopped() -> None:
    """After stop() the scheduler has no loop; the sweep returns immediately."""
    manager = get_manager()
    manager._auto_scheduler.stop()
    await manager.async_request_active_scan(duration=5.0)


@pytest.mark.asyncio
async def test_async_request_active_scan_no_op_without_auto_scanners() -> None:
    """With no AUTO workers the sweep still completes its sleep cleanly."""
    manager = get_manager()
    # No scanners registered; targets is empty but the sleep still runs.
    async with _no_real_sleep():
        await manager.async_request_active_scan(duration=5.0)
    assert manager._auto_scheduler._on_demand_sweep_future is None


@pytest.mark.asyncio
async def test_async_request_active_scan_awaits_the_full_duration() -> None:
    """
    The sweep awaits ``duration`` so the caller can read advertisements.

    Freezegun patches ``time.monotonic`` (and thus ``loop.time``);
    advancing the frozen clock by the requested duration lets the
    scheduler's internal ``asyncio.sleep`` complete and the task
    finish. The scanner is registered inside the freeze so the
    worker's ``_sweep_last_completed`` is anchored to the frozen
    clock; the worker's background ``_run`` task is cancelled so it
    cannot tick during the on-demand window.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    with freeze_time() as frozen:
        scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
        register_cancel = manager.async_register_scanner(scanner)
        worker = sched._workers[scanner.source]
        worker.stop()
        await asyncio.sleep(0)
        try:
            task = asyncio.create_task(manager.async_request_active_scan(duration=5.0))
            # Let the task start, flip the radio, and enter asyncio.sleep.
            for _ in range(5):
                await asyncio.sleep(0)
            assert scanner.active_window_calls == [5.0]
            assert not task.done()
            # Advance past the sweep duration; the sleep wakes up.
            frozen.tick(5.1)
            await task
            assert task.done()
        finally:
            register_cancel()


@pytest.mark.asyncio
async def test_async_request_active_scan_logs_per_scanner_flip_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A scanner whose flip raises is logged; the sweep still completes."""
    manager = get_manager()

    class _FailingScanner(_RecordingAutoScanner):
        async def async_request_active_window(self, duration: float) -> bool:
            msg = "boom"
            raise RuntimeError(msg)

    bad = _FailingScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    good = _RecordingAutoScanner("AA:BB:CC:DD:EE:01", BluetoothScanningMode.AUTO)
    c_bad = manager.async_register_scanner(bad)
    c_good = manager.async_register_scanner(good)
    try:
        with caplog.at_level(logging.WARNING, logger="habluetooth.auto_scheduler"):
            async with _no_real_sleep():
                await manager.async_request_active_scan(duration=5.0)
        assert good.active_window_calls == [5.0]
        assert any(
            "on-demand active window" in record.message and "boom" in record.message
            for record in caplog.records
        )
    finally:
        c_bad()
        c_good()


@pytest.mark.asyncio
async def test_async_request_active_scan_joiner_cancel_keeps_siblings() -> None:
    """
    Cancelling one joiner must not cancel the shared future.

    Without ``asyncio.shield`` on the joiner's await, a cancelled
    joiner would cancel the underlying future, which then propagates
    ``CancelledError`` to sibling joiners and makes the leader's
    ``finally`` raise ``InvalidStateError`` on ``set_result``.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        with freeze_time() as frozen:
            scanner._block_event = asyncio.Event()
            leader = asyncio.create_task(
                manager.async_request_active_scan(duration=5.0)
            )
            joiner_a = asyncio.create_task(
                manager.async_request_active_scan(duration=5.0)
            )
            joiner_b = asyncio.create_task(
                manager.async_request_active_scan(duration=5.0)
            )
            for _ in range(5):
                await asyncio.sleep(0)
            # Cancel one joiner; siblings and leader must continue.
            joiner_a.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await joiner_a
            scanner._block_event.set()
            frozen.tick(5.1)
            # Leader and the surviving joiner both complete normally.
            await leader
            await joiner_b
            assert joiner_b.result() is None
            assert sched._on_demand_sweep_future is None
    finally:
        register_cancel()


@pytest.mark.asyncio
async def test_async_request_active_scan_leader_cancellation_releases_joiners() -> None:
    """
    Cancelling the leader still resolves the future so joiners do not hang.

    Joiners see ``None`` (no propagated ``CancelledError``) and benefit
    from whatever radio activity already happened; a subsequent sweep can
    run because ``_on_demand_sweep_future`` is cleared.
    """
    manager = get_manager()
    sched = manager._auto_scheduler
    scanner = _RecordingAutoScanner("AA:BB:CC:DD:EE:00", BluetoothScanningMode.AUTO)
    register_cancel = manager.async_register_scanner(scanner)
    try:
        with freeze_time():
            # Block the leader inside its scanner-flip await so we know
            # we're past the gather and inside the leader's sleep when
            # we cancel.
            scanner._block_event = asyncio.Event()
            leader = asyncio.create_task(
                manager.async_request_active_scan(duration=5.0)
            )
            joiner = asyncio.create_task(
                manager.async_request_active_scan(duration=5.0)
            )
            for _ in range(5):
                await asyncio.sleep(0)
            # Both tasks are now waiting; joiner has latched onto the
            # leader's future.
            assert not leader.done()
            assert not joiner.done()
            scanner._block_event.set()
            leader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await leader
            # Joiner completes normally with no exception.
            await joiner
            assert joiner.result() is None
            # Future is cleared so a fresh sweep can start.
            assert sched._on_demand_sweep_future is None
    finally:
        register_cancel()
