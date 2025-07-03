import asyncio
import time
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import partial
from typing import Any
from unittest.mock import MagicMock, patch

from bleak.backends.scanner import AdvertisementData, BLEDevice

from habluetooth import get_manager
from habluetooth.models import BluetoothServiceInfoBleak

utcnow = partial(datetime.now, UTC)

HCI0_SOURCE_ADDRESS = "AA:BB:CC:DD:EE:00"
HCI1_SOURCE_ADDRESS = "AA:BB:CC:DD:EE:11"
NON_CONNECTABLE_REMOTE_SOURCE_ADDRESS = "AA:BB:CC:DD:EE:FF"

_MONOTONIC_RESOLUTION = time.get_clock_info("monotonic").resolution

ADVERTISEMENT_DATA_DEFAULTS = {
    "local_name": "Unknown",
    "manufacturer_data": {},
    "service_data": {},
    "service_uuids": [],
    "rssi": -127,
    "platform_data": ((),),
    "tx_power": -127,
}


BLE_DEVICE_DEFAULTS = {
    "name": None,
    "details": None,
}


def generate_advertisement_data(**kwargs: Any) -> AdvertisementData:
    """Generate advertisement data with defaults."""
    new = kwargs.copy()
    for key, value in ADVERTISEMENT_DATA_DEFAULTS.items():
        new.setdefault(key, value)
    return AdvertisementData(**new)


def generate_ble_device(
    address: str | None = None,
    name: str | None = None,
    details: Any | None = None,
    **kwargs: Any,
) -> BLEDevice:
    """Generate a BLEDevice with defaults."""
    new = kwargs.copy()
    if address is not None:
        new["address"] = address
    if name is not None:
        new["name"] = name
    if details is not None:
        new["details"] = details
    for key, value in BLE_DEVICE_DEFAULTS.items():
        new.setdefault(key, value)
    return BLEDevice(**new)


@contextmanager
def patch_bluetooth_time(mock_time: float) -> Generator[Any, None, None]:
    """Patch the bluetooth time."""
    with (
        patch("habluetooth.base_scanner.monotonic_time_coarse", return_value=mock_time),
        patch("habluetooth.manager.monotonic_time_coarse", return_value=mock_time),
        patch("habluetooth.scanner.monotonic_time_coarse", return_value=mock_time),
    ):
        yield


def async_fire_time_changed(utc_datetime: datetime) -> None:
    timestamp = utc_datetime.timestamp()
    loop = asyncio.get_running_loop()
    for task in list(loop._scheduled):  # type: ignore[attr-defined]
        if not isinstance(task, asyncio.TimerHandle):
            continue
        if task.cancelled():
            continue

        mock_seconds_into_future = timestamp - time.time()
        future_seconds = task.when() - (loop.time() + _MONOTONIC_RESOLUTION)

        if mock_seconds_into_future >= future_seconds:
            task._run()
            task.cancel()


class MockBleakClient:
    pass


def inject_advertisement(device: BLEDevice, adv: AdvertisementData) -> None:
    """Inject an advertisement into the manager."""
    return inject_advertisement_with_source(device, adv, "local")


def inject_advertisement_with_source(
    device: BLEDevice, adv: AdvertisementData, source: str
) -> None:
    """Inject an advertisement into the manager from a specific source."""
    inject_advertisement_with_time_and_source(device, adv, time.monotonic(), source)


def inject_advertisement_with_time_and_source(
    device: BLEDevice,
    adv: AdvertisementData,
    time: float,
    source: str,
) -> None:
    """Inject an advertisement into the manager from a specific source at a time."""
    inject_advertisement_with_time_and_source_connectable(
        device, adv, time, source, True
    )


def inject_advertisement_with_time_and_source_connectable(
    device: BLEDevice,
    adv: AdvertisementData,
    time: float,
    source: str,
    connectable: bool,
) -> None:
    """
    Inject an advertisement into the manager from a specific source at a time.

    As well as and connectable status.
    """
    manager = get_manager()

    manager.scanner_adv_received(
        BluetoothServiceInfoBleak(
            name=adv.local_name or device.name or device.address,
            address=device.address,
            rssi=adv.rssi,
            manufacturer_data=adv.manufacturer_data,
            service_data=adv.service_data,
            service_uuids=adv.service_uuids,
            source=source,
            device=device,
            advertisement=adv,
            connectable=connectable,
            time=time,
            tx_power=adv.tx_power,
        )
    )


@contextmanager
def patch_discovered_devices(
    mock_discovered: list[BLEDevice],
) -> Generator[None, None, None]:
    """Mock the combined best path to discovered devices from all the scanners."""
    manager = get_manager()
    original_all_history = manager._all_history
    original_connectable_history = manager._connectable_history
    manager._connectable_history = {}
    manager._all_history = {
        device.address: MagicMock(device=device) for device in mock_discovered
    }
    yield
    manager._all_history = original_all_history
    manager._connectable_history = original_connectable_history
