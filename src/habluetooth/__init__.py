__version__ = "0.4.0"

from .base_scanner import BaseHaRemoteScanner, BaseHaScanner
from .const import (
    CONNECTABLE_FALLBACK_MAXIMUM_STALE_ADVERTISEMENT_SECONDS,
    FALLBACK_MAXIMUM_STALE_ADVERTISEMENT_SECONDS,
    SCANNER_WATCHDOG_INTERVAL,
    SCANNER_WATCHDOG_TIMEOUT,
)
from .models import HaBluetoothConnector
from .scanner import BluetoothScanningMode, HaScanner

__all__ = [
    "BluetoothScanningMode",
    "HaScanner",
    "BaseHaScanner",
    "BaseHaRemoteScanner",
    "HaBluetoothConnector",
    "FALLBACK_MAXIMUM_STALE_ADVERTISEMENT_SECONDS",
    "CONNECTABLE_FALLBACK_MAXIMUM_STALE_ADVERTISEMENT_SECONDS",
    "SCANNER_WATCHDOG_TIMEOUT",
    "SCANNER_WATCHDOG_INTERVAL",
]
