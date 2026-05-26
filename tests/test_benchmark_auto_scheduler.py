"""Benchmarks for the auto-scan scheduler hot paths."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from habluetooth import (
    BaseHaScanner,
    BluetoothScanningMode,
    get_manager,
)
from habluetooth.const import CALLBACK_TYPE

from . import generate_advertisement_data, generate_ble_device

if TYPE_CHECKING:
    from collections.abc import Iterable

    from bleak.backends.scanner import AdvertisementData, BLEDevice
    from pytest_codspeed import BenchmarkFixture

pytestmark = pytest.mark.timeout(60)


class _AutoScanner(BaseHaScanner):
    """Minimal AUTO-mode scanner that exposes nothing to the discovery cache."""

    # Mirrors the _RecordingAutoScanner used by test_auto_scheduler.py but
    # without window-call tracking; the scheduler hot paths under benchmark
    # never enter the scanner's active-window path.

    __slots__ = ()

    def __init__(self, source: str) -> None:
        super().__init__(source, source, requested_mode=BluetoothScanningMode.AUTO)
        self.connectable = True

    async def async_request_active_window(self, duration: float) -> bool:
        return True

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


def _make_address(i: int) -> str:
    return f"AA:BB:CC:{(i >> 16) & 0xFF:02X}:{(i >> 8) & 0xFF:02X}:{i & 0xFF:02X}"


def _make_source(i: int) -> str:
    # Distinct source MACs so each scanner registers as its own worker.
    return f"DD:EE:FF:{(i >> 16) & 0xFF:02X}:{(i >> 8) & 0xFF:02X}:{i & 0xFF:02X}"


def _inject(scanner: _AutoScanner, address: str, now: float) -> None:
    """Drive a fake advertisement through the scanner's normal entry point."""
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
        now,
    )


def _setup_scheduler(
    num_scanners: int, num_devices: int
) -> tuple[list[_AutoScanner], list[CALLBACK_TYPE], list[CALLBACK_TYPE]]:
    """
    Register ``num_scanners`` AUTO scanners and ``num_devices`` scan requests.

    Each address is owned by exactly one scanner via a round-robin
    advertisement injection that populates manager history and
    auto_scheduler._needs (and the per-worker _owned_needs view on
    branches that have it).
    """
    manager = get_manager()
    loop = asyncio.get_running_loop()
    now = loop.time()
    scanners: list[_AutoScanner] = []
    scanner_cancels: list[CALLBACK_TYPE] = []
    for i in range(num_scanners):
        scanner = _AutoScanner(_make_source(i))
        scanners.append(scanner)
        scanner_cancels.append(manager.async_register_scanner(scanner))
    request_cancels: list[CALLBACK_TYPE] = []
    for i in range(num_devices):
        address = _make_address(i)
        request_cancels.append(
            manager.async_register_active_scan(address, scan_interval=120.0)
        )
        # Inject through the round-robin owner so manager history points
        # back to that scanner's source. The scheduler picks ownership
        # from history.source on both the old and new code paths.
        _inject(scanners[i % num_scanners], address, now)
    return scanners, scanner_cancels, request_cancels


def _teardown_scheduler(
    scanner_cancels: list[CALLBACK_TYPE],
    request_cancels: list[CALLBACK_TYPE],
) -> None:
    """Release scanner and active-scan registrations from ``_setup_scheduler``."""
    for cancel in request_cancels:
        cancel()
    for cancel in scanner_cancels:
        cancel()


@pytest.mark.asyncio
async def test_next_event_at_single_worker_8_scanners_200_devices(
    benchmark: BenchmarkFixture,
) -> None:
    """
    One worker computing its next wake among 8 scanners and 200 tracked devices.

    Prior to the per-worker owned-needs optimization (PR #508 / issue #506),
    every wake iterated the global ``_needs`` map (200 entries) and called
    ``async_last_service_info`` on each to filter by ownership. The
    optimization narrows the iteration to the ~25 entries the worker owns
    and removes the per-entry history lookup. This benchmark exercises the
    single-worker hot path so any regression in ``_next_event_at`` cost
    shows up immediately.
    """
    _, scanner_cancels, request_cancels = _setup_scheduler(
        num_scanners=8, num_devices=200
    )
    manager = get_manager()
    scheduler = manager._auto_scheduler
    worker = next(iter(scheduler._workers.values()))
    loop = asyncio.get_running_loop()

    @benchmark
    def run() -> None:
        worker._next_event_at(loop.time())

    _teardown_scheduler(scanner_cancels, request_cancels)


@pytest.mark.asyncio
async def test_next_event_at_burst_8_scanners_200_devices(
    benchmark: BenchmarkFixture,
) -> None:
    """
    All 8 workers compute their next wake — the burst scenario from issue #506.

    When an advertisement burst wakes every worker, the old code did
    O(K·N) work (K=8 workers each scanning N=200 entries). The
    optimization makes the total work O(N) because each worker only
    visits its owned subset. This benchmark captures the headline win.
    """
    _, scanner_cancels, request_cancels = _setup_scheduler(
        num_scanners=8, num_devices=200
    )
    manager = get_manager()
    scheduler = manager._auto_scheduler
    workers = list(scheduler._workers.values())
    loop = asyncio.get_running_loop()

    @benchmark
    def run() -> None:
        now = loop.time()
        for worker in workers:
            worker._next_event_at(now)

    _teardown_scheduler(scanner_cancels, request_cancels)


@pytest.mark.asyncio
async def test_collect_due_buckets_single_worker_8_scanners_200_devices(
    benchmark: BenchmarkFixture,
) -> None:
    """
    One worker collecting due buckets among 8 scanners and 200 devices.

    ``_collect_due_buckets`` shares the same iteration-scope problem as
    ``_next_event_at``: pre-#508 it iterated the global ``_needs`` and
    called ``async_last_service_info`` on every address to skip foreign
    owners; post-#508 it iterates the per-worker owned view directly.
    With entries scheduled well into the future, this exercises the
    no-dispatch read path that runs on every tick.
    """
    _, scanner_cancels, request_cancels = _setup_scheduler(
        num_scanners=8, num_devices=200
    )
    manager = get_manager()
    scheduler = manager._auto_scheduler
    worker = next(iter(scheduler._workers.values()))
    loop = asyncio.get_running_loop()

    @benchmark
    def run() -> None:
        worker._collect_due_buckets(loop.time())

    _teardown_scheduler(scanner_cancels, request_cancels)


@pytest.mark.asyncio
async def test_collect_due_buckets_burst_8_scanners_200_devices(
    benchmark: BenchmarkFixture,
) -> None:
    """All 8 workers collect due buckets — burst variant of the read path."""
    _, scanner_cancels, request_cancels = _setup_scheduler(
        num_scanners=8, num_devices=200
    )
    manager = get_manager()
    scheduler = manager._auto_scheduler
    workers = list(scheduler._workers.values())
    loop = asyncio.get_running_loop()

    @benchmark
    def run() -> None:
        now = loop.time()
        for worker in workers:
            worker._collect_due_buckets(now)

    _teardown_scheduler(scanner_cancels, request_cancels)
