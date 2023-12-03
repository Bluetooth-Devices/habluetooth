__version__ = "0.1.0"

from .base_scanner import BaseHaRemoteScanner, BaseHaScanner
from .const import (
    CONNECTABLE_FALLBACK_MAXIMUM_STALE_ADVERTISEMENT_SECONDS,
    FALLBACK_MAXIMUM_STALE_ADVERTISEMENT_SECONDS,
    SCANNER_WATCHDOG_INTERVAL,
    SCANNER_WATCHDOG_TIMEOUT,
)
from .models import HaBluetoothConnector

__all__ = [
    "BaseHaScanner",
    "BaseHaRemoteScanner",
    "HaBluetoothConnector",
    "FALLBACK_MAXIMUM_STALE_ADVERTISEMENT_SECONDS",
    "CONNECTABLE_FALLBACK_MAXIMUM_STALE_ADVERTISEMENT_SECONDS",
    "SCANNER_WATCHDOG_TIMEOUT",
    "SCANNER_WATCHDOG_INTERVAL",
]
