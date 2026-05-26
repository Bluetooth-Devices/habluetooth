"""Benchmarks for the BluetoothManager hot paths."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from bluetooth_data_tools import monotonic_time_coarse

from habluetooth import (
    BaseHaRemoteScanner,
    BaseHaScanner,
    HaBluetoothConnector,
    get_manager,
)
from habluetooth.models import BluetoothServiceInfoBleak

from . import (
    MockBleakClient,
    generate_advertisement_data,
    generate_ble_device,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from bleak.backends.scanner import AdvertisementData, BLEDevice
    from pytest_codspeed import BenchmarkFixture

pytestmark = pytest.mark.timeout(60)


class _LocalScannerLike(BaseHaScanner):
    """Stand-in for HaScanner that rebuilds its discovered-dict each access."""

    # The real HaScanner.discovered_addresses delegates to
    # bleak.BleakScanner.discovered_devices_and_advertisement_data which
    # walks bleak's backend cache and constructs a new dict each call. This
    # fake reproduces that allocation pattern so the benchmarks reflect the
    # redundant-rebuild cost that issue #505 targets — without depending on
    # a live BlueZ stack.

    def __init__(self, source: str, adapter: str, addresses: list[str]) -> None:
        super().__init__(source, adapter, connectable=True)
        self._addresses = addresses
        self._device = generate_ble_device(
            addresses[0] if addresses else "00:00:00:00:00:00", "x", {}
        )
        self._adv = generate_advertisement_data(local_name="x")

    @property
    def discovered_devices(self) -> list[BLEDevice]:
        return [self._device for _ in self._addresses]

    @property
    def discovered_devices_and_advertisement_data(
        self,
    ) -> dict[str, tuple[BLEDevice, AdvertisementData]]:
        # Rebuild the dict on every access, matching bleak's behavior.
        return dict.fromkeys(self._addresses, (self._device, self._adv))

    @property
    def discovered_addresses(self) -> Iterable[str]:
        # Match HaScanner: dict iteration yields keys, but the dict is
        # rebuilt on every access.
        return self.discovered_devices_and_advertisement_data

    def get_discovered_device_advertisement_data(
        self, address: str
    ) -> tuple[BLEDevice, AdvertisementData] | None:
        return self.discovered_devices_and_advertisement_data.get(address)


def _make_address(i: int) -> str:
    return f"AA:BB:CC:{(i >> 16) & 0xFF:02X}:{(i >> 8) & 0xFF:02X}:{i & 0xFF:02X}"


def _seed_history(num_devices: int, source: str) -> list[str]:
    """Populate manager history with ``num_devices`` devices from ``source``."""
    manager = get_manager()
    now = monotonic_time_coarse()
    addresses: list[str] = []
    for i in range(num_devices):
        address = _make_address(i)
        addresses.append(address)
        device = generate_ble_device(address, f"dev{i}", {})
        adv = generate_advertisement_data(
            local_name=f"dev{i}",
            manufacturer_data={1: bytes((i & 0xFF,))},
            service_uuids=[],
            rssi=-60,
        )
        manager.scanner_adv_received(
            BluetoothServiceInfoBleak(
                name=adv.local_name,
                address=address,
                rssi=adv.rssi,
                manufacturer_data=adv.manufacturer_data,
                service_data=adv.service_data,
                service_uuids=adv.service_uuids,
                source=source,
                device=device,
                advertisement=adv,
                connectable=True,
                time=now,
                tx_power=adv.tx_power,
            )
        )
    return addresses


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_check_unavailable_steady_state_remote(
    benchmark: BenchmarkFixture,
) -> None:
    """Steady-state _async_check_unavailable with one remote scanner and 200 devices."""
    # Nothing has disappeared — every history address is still in the scanner's
    # discovered_addresses. This is the dominant production path: each cycle
    # runs the difference twice (connectable + non-connectable loops).
    manager = get_manager()
    connector = HaBluetoothConnector(MockBleakClient, "mock_bleak_client", lambda: True)
    scanner = BaseHaRemoteScanner("esp32", "esp32", connector, connectable=True)
    unsetup = scanner.async_setup()
    cancel = manager.async_register_scanner(scanner)

    addresses = _seed_history(200, "esp32")
    # Inject through the remote scanner's normal entry point so its
    # discovered_addresses (== _previous_service_info) is populated too.
    now = monotonic_time_coarse()
    for i, address in enumerate(addresses):
        scanner._async_on_advertisement(
            address,
            -60,
            f"dev{i}",
            [],
            {},
            {1: bytes((i & 0xFF,))},
            None,
            {"scanner_specific_data": "test"},
            now,
        )

    @benchmark
    def run() -> None:
        manager._async_check_unavailable()

    cancel()
    unsetup()


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_check_unavailable_steady_state_local_like(
    benchmark: BenchmarkFixture,
) -> None:
    """Steady-state _async_check_unavailable with one rebuilding local-like scanner."""
    # The local-like scanner rebuilds its discovered_addresses dict on every
    # property access. This isolates the cost issue #505 calls out:
    # _async_check_unavailable invokes discovered_addresses on every
    # connectable scanner twice per cycle, and for local HaScanner each
    # access rebuilds bleak's discovered-devices dict.
    manager = get_manager()
    addresses = _seed_history(200, "hci0")
    scanner = _LocalScannerLike("hci0", "hci0", addresses)
    cancel = manager.async_register_scanner(scanner)

    @benchmark
    def run() -> None:
        manager._async_check_unavailable()

    cancel()


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_check_unavailable_many_scanners_local_like(
    benchmark: BenchmarkFixture,
) -> None:
    """Steady-state _async_check_unavailable: four local-like + one remote scanner."""
    # 200 devices total. Multi-scanner deployments amplify the redundant-rebuild
    # cost: every connectable scanner's discovered_addresses is materialized
    # twice per cycle (issue #505).
    manager = get_manager()
    addresses = _seed_history(200, "hci0")

    scanners: list[BaseHaScanner] = []
    cancels = []
    for idx, adapter in enumerate(("hci0", "hci1", "hci2", "hci3")):
        # Each local-like scanner sees a non-empty overlapping slice so the
        # set difference work mirrors a real multi-adapter install.
        slice_size = max(1, len(addresses) // (idx + 1))
        local = _LocalScannerLike(adapter, adapter, addresses[:slice_size])
        scanners.append(local)
        cancels.append(manager.async_register_scanner(local))

    connector = HaBluetoothConnector(
        MockBleakClient, "mock_bleak_client", lambda: False
    )
    remote = BaseHaRemoteScanner("esp32_nc", "esp32_nc", connector, connectable=False)
    remote_unsetup = remote.async_setup()
    cancels.append(manager.async_register_scanner(remote))

    @benchmark
    def run() -> None:
        manager._async_check_unavailable()

    for c in cancels:
        c()
    remote_unsetup()


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_check_unavailable_all_disappeared(
    benchmark: BenchmarkFixture,
) -> None:
    """Worst case: every history address has disappeared from every scanner."""
    # Exercises the disappear-callback and tracker-cleanup branches of the
    # inner loop. Re-seeds history each iteration so the benchmark measures
    # the dispatch work, not a one-shot drain. 50 devices is small on purpose
    # to keep the re-seed overhead from dominating.
    manager = get_manager()
    connector = HaBluetoothConnector(MockBleakClient, "mock_bleak_client", lambda: True)
    scanner = BaseHaRemoteScanner("esp32", "esp32", connector, connectable=True)
    unsetup = scanner.async_setup()
    cancel = manager.async_register_scanner(scanner)

    template_addresses = [_make_address(i) for i in range(50)]

    def reseed() -> None:
        manager._all_history.clear()
        manager._connectable_history.clear()
        now = monotonic_time_coarse() - 10_000  # ensure beyond stale threshold
        for i, address in enumerate(template_addresses):
            device = generate_ble_device(address, f"dev{i}", {})
            adv = generate_advertisement_data(
                local_name=f"dev{i}",
                manufacturer_data={1: bytes((i & 0xFF,))},
                service_uuids=[],
                rssi=-60,
            )
            manager.scanner_adv_received(
                BluetoothServiceInfoBleak(
                    name=adv.local_name,
                    address=address,
                    rssi=adv.rssi,
                    manufacturer_data=adv.manufacturer_data,
                    service_data=adv.service_data,
                    service_uuids=adv.service_uuids,
                    source="esp32",
                    device=device,
                    advertisement=adv,
                    connectable=True,
                    time=now,
                    tx_power=adv.tx_power,
                )
            )

    @benchmark
    def run() -> None:
        reseed()
        manager._async_check_unavailable()

    cancel()
    unsetup()
