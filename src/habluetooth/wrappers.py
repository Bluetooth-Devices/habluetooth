"""Bleak wrappers for bluetooth."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any, Final, Literal, overload

from bleak import BleakClient, BleakError
from bleak.backends.client import BaseBleakClient, get_platform_client_backend_type
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import (
    AdvertisementData,
    AdvertisementDataCallback,
    BaseBleakScanner,
)
from bleak_retry_connector import (
    ble_device_description,
    clear_cache,
    device_source,
)

from .central_manager import get_manager
from .const import CALLBACK_TYPE

FILTER_UUIDS: Final = "UUIDs"
_LOGGER = logging.getLogger(__name__)


if TYPE_CHECKING:
    from .base_scanner import BaseHaScanner
    from .manager import BluetoothManager


@dataclass(slots=True)
class _HaWrappedBleakBackend:
    """Wrap bleak backend to make it usable by Home Assistant."""

    device: BLEDevice
    scanner: BaseHaScanner
    client: type[BaseBleakClient]
    source: str | None


class HaBleakScannerWrapper(BaseBleakScanner):
    """A wrapper that uses the single instance."""

    def __init__(
        self,
        *args: Any,
        detection_callback: AdvertisementDataCallback | None = None,
        service_uuids: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the BleakScanner."""
        self._detection_cancel: CALLBACK_TYPE | None = None
        self._mapped_filters: dict[str, set[str]] = {}
        self._advertisement_data_callback: AdvertisementDataCallback | None = None
        self._background_tasks: set[asyncio.Task[Any]] = set()
        remapped_kwargs = {
            "detection_callback": detection_callback,
            "service_uuids": service_uuids or [],
            **kwargs,
        }
        self._map_filters(*args, **remapped_kwargs)
        super().__init__(
            detection_callback=detection_callback, service_uuids=service_uuids or []
        )

    @classmethod
    async def find_device_by_address(
        cls, device_identifier: str, timeout: float = 10.0, **kwargs: Any
    ) -> BLEDevice | None:
        """Find a device by address."""
        manager = get_manager()
        return manager.async_ble_device_from_address(
            device_identifier, True
        ) or manager.async_ble_device_from_address(device_identifier, False)

    @overload
    @classmethod
    async def discover(
        cls, timeout: float = 5.0, *, return_adv: Literal[False] = False, **kwargs: Any
    ) -> list[BLEDevice]: ...

    @overload
    @classmethod
    async def discover(
        cls, timeout: float = 5.0, *, return_adv: Literal[True], **kwargs: Any
    ) -> dict[str, tuple[BLEDevice, AdvertisementData]]: ...

    @classmethod
    async def discover(
        cls, timeout: float = 5.0, *, return_adv: bool = False, **kwargs: Any
    ) -> list[BLEDevice] | dict[str, tuple[BLEDevice, AdvertisementData]]:
        """Discover devices."""
        infos = get_manager().async_discovered_service_info(True)
        if return_adv:
            return {info.address: (info.device, info.advertisement) for info in infos}
        return [info.device for info in infos]

    async def stop(self, *args: Any, **kwargs: Any) -> None:
        """Stop scanning for devices."""

    async def start(self, *args: Any, **kwargs: Any) -> None:
        """Start scanning for devices."""

    def _map_filters(self, *args: Any, **kwargs: Any) -> bool:
        """Map the filters."""
        mapped_filters = {}
        if filters := kwargs.get("filters"):
            if filter_uuids := filters.get(FILTER_UUIDS):
                mapped_filters[FILTER_UUIDS] = set(filter_uuids)
            else:
                _LOGGER.warning("Only %s filters are supported", FILTER_UUIDS)
        if service_uuids := kwargs.get("service_uuids"):
            mapped_filters[FILTER_UUIDS] = set(service_uuids)
        if mapped_filters == self._mapped_filters:
            return False
        self._mapped_filters = mapped_filters
        return True

    def set_scanning_filter(self, *args: Any, **kwargs: Any) -> None:
        """Set the filters to use."""
        if self._map_filters(*args, **kwargs):
            self._setup_detection_callback()

    def _cancel_callback(self) -> None:
        """Cancel callback."""
        if self._detection_cancel:
            self._detection_cancel()
            self._detection_cancel = None

    @property
    def discovered_devices(self) -> list[BLEDevice]:
        """Return a list of discovered devices."""
        return list(get_manager().async_discovered_devices(True))

    def register_detection_callback(
        self, callback: AdvertisementDataCallback | None
    ) -> Callable[[], None]:
        """
        Register a detection callback.

        The callback is called when a device is discovered or has a property changed.

        This method takes the callback and registers it with the long running scanner.
        """
        self._advertisement_data_callback = callback
        self._setup_detection_callback()
        if TYPE_CHECKING:
            assert self._detection_cancel is not None
        return self._detection_cancel

    def _setup_detection_callback(self) -> None:
        """Set up the detection callback."""
        if self._advertisement_data_callback is None:
            return
        callback = self._advertisement_data_callback
        self._cancel_callback()
        super().register_detection_callback(self._advertisement_data_callback)
        manager = get_manager()

        if not inspect.iscoroutinefunction(callback):
            detection_callback = callback
        else:

            def detection_callback(
                ble_device: BLEDevice, advertisement_data: AdvertisementData
            ) -> None:
                task = asyncio.create_task(callback(ble_device, advertisement_data))
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)

        self._detection_cancel = manager.async_register_bleak_callback(
            detection_callback, self._mapped_filters
        )

    def __del__(self) -> None:
        """Delete the BleakScanner."""
        if self._detection_cancel:
            # Nothing to do if event loop is already closed
            with contextlib.suppress(RuntimeError):
                asyncio.get_running_loop().call_soon_threadsafe(self._detection_cancel)


class HaBleakClientWrapper(BleakClient):
    """
    Wrap the BleakClient to ensure it does not shutdown our scanner.

    If an address is passed into BleakClient instead of a BLEDevice,
    bleak will quietly start a new scanner under the hood to resolve
    the address. This can cause a conflict with our scanner. We need
    to handle translating the address to the BLEDevice in this case
    to avoid the whole stack from getting stuck in an in progress state
    when an integration does this.
    """

    def __init__(  # pylint: disable=super-init-not-called
        self,
        address_or_ble_device: str | BLEDevice,
        disconnected_callback: Callable[[BleakClient], None] | None = None,
        *args: Any,
        timeout: float = 10.0,
        **kwargs: Any,
    ) -> None:
        """Initialize the BleakClient."""
        if isinstance(address_or_ble_device, BLEDevice):
            self.__address = address_or_ble_device.address
        else:
            # If we are passed an address we need to make sure
            # its not a subclassed str
            self.__address = str(address_or_ble_device)
        self.__disconnected_callback = disconnected_callback
        self.__manager = get_manager()
        self.__timeout = timeout
        self._backend: BaseBleakClient | None = None

    @property
    def is_connected(self) -> bool:
        """Return True if the client is connected to a device."""
        return self._backend is not None and self._backend.is_connected

    async def clear_cache(self) -> bool:
        """Clear the GATT cache."""
        if self._backend is not None and hasattr(self._backend, "clear_cache"):
            return await self._backend.clear_cache()
        return await clear_cache(self.__address)

    def set_disconnected_callback(
        self,
        callback: Callable[[BleakClient], None] | None,
        **kwargs: Any,
    ) -> None:
        """Set the disconnect callback."""
        self.__disconnected_callback = callback
        if self._backend:
            self._backend.set_disconnected_callback(
                self._make_disconnected_callback(callback),
                **kwargs,
            )

    def _make_disconnected_callback(
        self, callback: Callable[[BleakClient], None] | None
    ) -> Callable[[], None] | None:
        """
        Make the disconnected callback.

        https://github.com/hbldh/bleak/pull/1256
        The disconnected callback needs to get the top level
        BleakClientWrapper instance, not the backend instance.

        The signature of the callback for the backend is:
            Callable[[], None]

        To make this work we need to wrap the callback in a partial
        that passes the BleakClientWrapper instance as the first
        argument.
        """
        return None if callback is None else partial(callback, self)

    async def connect(self, **kwargs: Any) -> bool:
        """Connect to the specified GATT server."""
        manager = self.__manager
        if manager.shutdown:
            raise BleakError("Bluetooth is already shutdown")
        if debug_logging := _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("%s: Looking for backend to connect", self.__address)
        wrapped_backend = self._async_get_best_available_backend_and_device(manager)
        device = wrapped_backend.device
        scanner = wrapped_backend.scanner
        self._backend = wrapped_backend.client(
            device,
            disconnected_callback=self._make_disconnected_callback(
                self.__disconnected_callback
            ),
            timeout=self.__timeout,
        )
        if debug_logging:
            # Only lookup the description if we are going to log it
            description = ble_device_description(device)
            device_adv = scanner.get_discovered_device_advertisement_data(
                device.address
            )
            if TYPE_CHECKING:
                assert device_adv is not None
            adv = device_adv[1]
            rssi = adv.rssi
            _LOGGER.debug(
                "%s: Connecting via %s (last rssi: %s)", description, scanner.name, rssi
            )
        connected = False
        address = device.address
        try:
            scanner._add_connecting(address)
            connected = await super().connect(**kwargs)
        finally:
            scanner._finished_connecting(address, connected)
            # If we failed to connect and its a local adapter (no source)
            # we release the connection slot
            if not connected and not wrapped_backend.source:
                manager.async_release_connection_slot(device)

        if debug_logging:
            _LOGGER.debug(
                "%s: %s via %s (last rssi: %s)",
                description,
                "Connected" if connected else "Failed to connect",
                scanner.name,
                rssi,
            )
        return connected

    def _async_get_backend_for_ble_device(
        self, manager: BluetoothManager, scanner: BaseHaScanner, ble_device: BLEDevice
    ) -> _HaWrappedBleakBackend | None:
        """Get the backend for a BLEDevice."""
        if not (source := device_source(ble_device)):
            # If client is not defined in details
            # its the client for this platform
            if not manager.async_allocate_connection_slot(ble_device):
                return None
            cls = get_platform_client_backend_type()
            return _HaWrappedBleakBackend(ble_device, scanner, cls, source)

        # Make sure the backend can connect to the device
        # as some backends have connection limits
        if not scanner.connector or not scanner.connector.can_connect():
            return None

        return _HaWrappedBleakBackend(
            ble_device, scanner, scanner.connector.client, source
        )

    def _async_get_best_available_backend_and_device(
        self, manager: BluetoothManager
    ) -> _HaWrappedBleakBackend:
        """
        Get a best available backend and device for the given address.

        This method will return the backend with the best rssi
        that has a free connection slot.
        """
        address = self.__address
        sorted_devices = sorted(
            manager.async_scanner_devices_by_address(self.__address, True),
            key=lambda x: x.advertisement.rssi,
            reverse=True,
        )
        if len(sorted_devices) > 1:
            rssi_diff = (
                sorted_devices[0].advertisement.rssi
                - sorted_devices[1].advertisement.rssi
            )
            sorted_devices = sorted(
                sorted_devices,
                key=lambda device: device.score_connection_path(rssi_diff),
                reverse=True,
            )

        if sorted_devices and _LOGGER.isEnabledFor(logging.INFO):
            _LOGGER.info(
                "%s - %s: Found %s connection path(s), preferred order: %s",
                address,
                sorted_devices[0].ble_device.name,
                len(sorted_devices),
                ", ".join(
                    f"{device.scanner.name} "
                    f"(RSSI={device.advertisement.rssi}) "
                    f"(failures={device.scanner._connection_failures(address)}) "
                    f"(in_progress={device.scanner._connections_in_progress()}) "
                    f"(score={device.score_connection_path(0)})"
                    for device in sorted_devices
                ),
            )

        for device in sorted_devices:
            if backend := self._async_get_backend_for_ble_device(
                manager, device.scanner, device.ble_device
            ):
                return backend

        raise BleakError(
            "No backend with an available connection slot that can reach address"
            f" {address} was found"
        )

    async def disconnect(self) -> bool:
        """Disconnect from the device."""
        if self._backend is None:
            return True
        return await self._backend.disconnect()
