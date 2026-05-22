__version__ = "6.2.0"

from bleak_retry_connector import Allocations

# isort: off
# Order matters: manager must finish initializing before base_scanner
# imports it, otherwise macOS Cython hits KeyError: '__pyx_vtable__'
# resolving BluetoothManager from a partially-initialized module.
from .advertisement_tracker import (
    TRACKER_BUFFERING_WOBBLE_SECONDS,
    AdvertisementTracker,
)
from .manager import BluetoothManager
from .base_scanner import BaseHaRemoteScanner, BaseHaScanner

# isort: on
from .central_manager import get_manager, set_manager
from .const import (
    CONNECTABLE_FALLBACK_MAXIMUM_STALE_ADVERTISEMENT_SECONDS,
    FALLBACK_MAXIMUM_STALE_ADVERTISEMENT_SECONDS,
    SCANNER_WATCHDOG_INTERVAL,
    SCANNER_WATCHDOG_TIMEOUT,
    UNAVAILABLE_TRACK_SECONDS,
)
from .models import (
    BluetoothServiceInfo,
    BluetoothServiceInfoBleak,
    HaBluetoothConnector,
    HaBluetoothSlotAllocations,
    HaScannerDetails,
    HaScannerModeChange,
    HaScannerRegistration,
    HaScannerRegistrationEvent,
    HaScannerType,
)
from .scanner import BluetoothScanningMode, HaScanner, ScannerStartError
from .scanner_device import BluetoothScannerDevice
from .storage import (
    DiscoveredDeviceAdvertisementData,
    DiscoveredDeviceAdvertisementDataDict,
    DiscoveryStorageType,
    discovered_device_advertisement_data_from_dict,
    discovered_device_advertisement_data_to_dict,
    expire_stale_scanner_discovered_device_advertisement_data,
)
from .wrappers import HaBleakClientWrapper, HaBleakScannerWrapper

__all__ = [
    "CONNECTABLE_FALLBACK_MAXIMUM_STALE_ADVERTISEMENT_SECONDS",
    "FALLBACK_MAXIMUM_STALE_ADVERTISEMENT_SECONDS",
    "SCANNER_WATCHDOG_INTERVAL",
    "SCANNER_WATCHDOG_TIMEOUT",
    "TRACKER_BUFFERING_WOBBLE_SECONDS",
    "UNAVAILABLE_TRACK_SECONDS",
    "AdvertisementTracker",
    "Allocations",
    "BaseHaRemoteScanner",
    "BaseHaScanner",
    "BluetoothManager",
    "BluetoothScannerDevice",
    "BluetoothScanningMode",
    "BluetoothServiceInfo",
    "BluetoothServiceInfoBleak",
    "DiscoveredDeviceAdvertisementData",
    "DiscoveredDeviceAdvertisementDataDict",
    "DiscoveryStorageType",
    "HaBleakClientWrapper",
    "HaBleakScannerWrapper",
    "HaBluetoothConnector",
    "HaBluetoothSlotAllocations",
    "HaScanner",
    "HaScannerDetails",
    "HaScannerModeChange",
    "HaScannerRegistration",
    "HaScannerRegistrationEvent",
    "HaScannerType",
    "ScannerStartError",
    "discovered_device_advertisement_data_from_dict",
    "discovered_device_advertisement_data_to_dict",
    "expire_stale_scanner_discovered_device_advertisement_data",
    "get_manager",
    "set_manager",
]
