"""Base classes for HA Bluetooth scanners for bluetooth."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from .const import (
    SCANNER_WATCHDOG_INTERVAL,
)

SCANNER_WATCHDOG_INTERVAL_SECONDS: Final = SCANNER_WATCHDOG_INTERVAL.total_seconds()
_LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .base_scanner import BaseHaScanner


@dataclass(slots=True)
class BluetoothScannerDevice:
    """Data for a bluetooth device from a given scanner."""

    scanner: BaseHaScanner
    ble_device: BLEDevice
    advertisement: AdvertisementData
