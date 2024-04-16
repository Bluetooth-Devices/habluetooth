from contextlib import contextmanager
from typing import Any, Generator
from unittest.mock import patch

from bleak.backends.scanner import AdvertisementData, BLEDevice

ADVERTISEMENT_DATA_DEFAULTS = {
    "manufacturer_data": {},
    "service_data": {},
    "service_uuids": [],
    "rssi": -127,
    "platform_data": ((),),
    "tx_power": -127,
}


BLE_DEVICE_DEFAULTS = {
    "name": None,
    "rssi": -127,
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
    rssi: int | None = None,
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
    if rssi is not None:
        new["rssi"] = rssi
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
