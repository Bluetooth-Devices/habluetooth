"""Benchmarks for the base scanner."""

from __future__ import annotations

import pytest
from bluetooth_data_tools import monotonic_time_coarse
from pytest_codspeed import BenchmarkFixture

from habluetooth import BaseHaRemoteScanner, HaBluetoothConnector, get_manager

from . import (
    MockBleakClient,
    generate_advertisement_data,
    generate_ble_device,
)


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_inject_100_simple_advertisments(benchmark: BenchmarkFixture) -> None:
    """Test the remote scanner base class merges advertisement_data."""
    manager = get_manager()

    switchbot_device = generate_ble_device(
        "44:44:33:11:23:45",
        "wohand",
        {},
        rssi=-100,
    )
    switchbot_device_adv = generate_advertisement_data(
        local_name="wohand",
        service_uuids=["050a021a-0000-1000-8000-00805f9b34fb"],
        service_data={"050a021a-0000-1000-8000-00805f9b34fb": b"\n\xff"},
        manufacturer_data={1: b"\x01"},
        rssi=-100,
    )

    connector = HaBluetoothConnector(
        MockBleakClient, "mock_bleak_client", lambda: False
    )
    scanner = BaseHaRemoteScanner("esp32", "esp32", connector, True)
    unsetup = scanner.async_setup()
    cancel = manager.async_register_scanner(scanner)

    def run():
        _address = switchbot_device.address
        _rssi = switchbot_device_adv.rssi
        _name = switchbot_device.name
        _service_uuids = switchbot_device_adv.service_uuids
        _service_data = switchbot_device_adv.service_data
        _manufacturer_data = switchbot_device_adv.manufacturer_data
        _tx_power = switchbot_device_adv.tx_power
        _details = {"scanner_specific_data": "test"}
        _now = monotonic_time_coarse()

        for _ in range(100):
            scanner._async_on_advertisement(
                _address,
                _rssi,
                _name,
                _service_uuids,
                _service_data,
                _manufacturer_data,
                _tx_power,
                _details,
                _now,
            )

    benchmark(run)

    cancel()
    unsetup()
