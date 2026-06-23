"""
Local scanner driven entirely by the BlueZ management socket.

EXPERIMENTAL: this module and its public names (``HaScannerMgmt`` and
``create_local_scanner``) are a work in progress. The API is not stable and is
likely to change or be renamed; nothing in Home Assistant selects this scanner
yet, so do not depend on it.

``HaScannerMgmt`` is the DBus-free counterpart to :class:`HaScanner`: it drives
discovery through the kernel management socket (active discovery or a passive
advertisement monitor) and routes connections through :class:`HaMgmtClient`
over a raw L2CAP ATT channel. Advertisements arrive via the shared mgmt side
channel, which the manager points at this scanner once it is registered (keyed
by ``adapter_idx``), so the hot advert path is inherited unchanged from
:class:`BaseHaScanner`.

Connection slots are tracked here rather than in ``BleakSlotManager``: that
manager counts connections by BlueZ DBus path, which an L2CAP connection never
creates, so it cannot see mgmt connections. This scanner keeps its own set of
live peer addresses and gates ``can_connect`` against the configured slot count
(read from the slot manager purely as the limit).

``create_local_scanner`` is the factory Home Assistant will call instead of
constructing a scanner directly; it returns this scanner when mgmt discovery is
available and falls back to the bleak-backed :class:`HaScanner` otherwise. Home
Assistant is not wired to the factory yet, so nothing selects this scanner in
production.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import TYPE_CHECKING, cast

from bleak_retry_connector import Allocations
from bluetooth_adapters import DEFAULT_ADDRESS

from .base_scanner import BaseHaScanner
from .central_manager import get_manager
from .client_mgmt import HaMgmtClient, MgmtClientData
from .models import BluetoothScanningMode, HaBluetoothConnector
from .scanner_bleak import IS_LINUX, HaScanner, ScannerStartError, _resolve_radio_mode

if TYPE_CHECKING:
    from collections.abc import Coroutine, Iterable
    from typing import Any

    from bleak.backends.client import BaseBleakClient
    from bleak.backends.device import BLEDevice
    from bleak.backends.scanner import AdvertisementData

    from .const import CALLBACK_TYPE

_LOGGER = logging.getLogger(__name__)


class _MgmtClientFactory(partial):  # type: ignore[type-arg]
    """A functools.partial that binds per-connection data into HaMgmtClient."""


# The connect path derives the backend id from
# ``type(scanner.connector.client).__name__`` (wrappers.py); a bare partial
# reads as the generic "partial", so name this factory after the client it
# builds for clear connect diagnostics.
_MgmtClientFactory.__name__ = "HaMgmtClient"
_MgmtClientFactory.__qualname__ = "HaMgmtClient"


class HaScannerMgmt(BaseHaScanner):
    """A local scanner that discovers and connects over the BlueZ mgmt socket."""

    def __init__(self, mode: BluetoothScanningMode, adapter: str, address: str) -> None:
        """Set up the scanner and wire connections to the mgmt GATT client."""
        if address == DEFAULT_ADDRESS:
            # The L2CAP connect path binds to this address to pin connections to
            # the discovering adapter; without a real BD_ADDR it would bind to
            # BDADDR_ANY and route through the wrong radio. Fail fast rather than
            # mis-route (the factory already guards against this).
            msg = "HaScannerMgmt requires a real adapter address, not DEFAULT_ADDRESS"
            raise ValueError(msg)
        self.mac_address = address
        source = address
        connector = HaBluetoothConnector(
            # partial-bind the per-connection data; the wrapper calls
            # connector.client(device, ...) like any backend class.
            client=cast(
                "type[BaseBleakClient]",
                _MgmtClientFactory(
                    HaMgmtClient,
                    client_data=MgmtClientData(
                        adapter_address=address,
                        scanner=self,
                        register_connection=self._register_connection,
                        unregister_connection=self._unregister_connection,
                    ),
                ),
            ),
            source=source,
            can_connect=self._can_connect,
        )
        super().__init__(source, adapter, connector=connector, requested_mode=mode)
        self.connectable = True
        self.scanning = False
        self._start_stop_lock = asyncio.Lock()
        self._monitor_handle: int | None = None
        self._connections: set[str] = set()
        self._background_tasks: set[asyncio.Task[Any]] = set()

    # -- lifecycle ---------------------------------------------------------
    def async_setup(self) -> CALLBACK_TYPE:
        """Set up the scanner and return the teardown callback."""
        super().async_setup()
        return self._unsetup

    def _unsetup(self) -> None:
        """Tear down the expiry timer and the watchdog."""
        super()._unsetup()
        self._async_stop_scanner_watchdog()

    async def async_start(self) -> None:
        """Start mgmt discovery for this adapter."""
        async with self._start_stop_lock:
            await self._async_start()

    async def _async_start(self) -> None:
        """Issue the discovery/monitor command and arm the watchdog."""
        idx = self.adapter_idx
        mgmt = get_manager().get_bluez_mgmt_ctl()
        if idx is None or mgmt is None or not mgmt.can_discover:
            msg = f"{self.name}: mgmt discovery is not available"
            raise ScannerStartError(msg)
        mode = _resolve_radio_mode(self.requested_mode or BluetoothScanningMode.PASSIVE)
        if mode is BluetoothScanningMode.ACTIVE:
            if not await mgmt.start_discovery(idx):
                msg = f"{self.name}: failed to start mgmt discovery"
                raise ScannerStartError(msg)
        else:
            handle = await mgmt.add_adv_pattern_monitor(idx)
            if handle is None:
                msg = f"{self.name}: failed to add advertisement monitor"
                raise ScannerStartError(msg)
            self._monitor_handle = handle
        # Use the setter so mode-change subscribers are notified, matching
        # HaScanner.
        self.set_current_mode(mode)
        self.scanning = True
        self._async_setup_scanner_watchdog()
        self._on_start_success()

    async def async_stop(self) -> None:
        """Stop mgmt discovery and the watchdog."""
        async with self._start_stop_lock:
            self._async_stop_scanner_watchdog()
            await self._async_stop_discovery()
            self.scanning = False

    async def _async_stop_discovery(self) -> None:
        """Stop the active discovery or remove the passive monitor."""
        idx = self.adapter_idx
        mgmt = get_manager().get_bluez_mgmt_ctl()
        if idx is None or mgmt is None:
            # The controller went away with discovery possibly still running in
            # the kernel; log so the orphaned state is observable.
            _LOGGER.debug(
                "%s: cannot stop discovery, mgmt controller unavailable", self.name
            )
            return
        if self._monitor_handle is not None:
            if await mgmt.remove_adv_monitor(idx, self._monitor_handle):
                self._monitor_handle = None
            else:
                # Keep the handle so a later stop can retry rather than leak it.
                _LOGGER.warning(
                    "%s: failed to remove advertisement monitor %s",
                    self.name,
                    self._monitor_handle,
                )
        elif not await mgmt.stop_discovery(idx):
            _LOGGER.warning("%s: failed to stop mgmt discovery", self.name)

    def _async_scanner_watchdog(self) -> None:
        """Restart discovery if the adapter has gone quiet."""
        if not self._async_watchdog_triggered():
            return
        if self._start_stop_lock.locked():
            return
        _LOGGER.debug(
            "%s: mgmt scanner quiet for %ss, restarting discovery",
            self.name,
            self.time_since_last_detection(),
        )
        self.scanning = False
        self._create_background_task(self._async_restart())

    async def _async_restart(self) -> None:
        """Stop and start discovery again under the start/stop lock."""
        async with self._start_stop_lock:
            await self._async_stop_discovery()
            try:
                await self._async_start()
            except ScannerStartError:
                _LOGGER.exception("%s: failed to restart mgmt discovery", self.name)

    def _create_background_task(self, coro: Coroutine[Any, Any, None]) -> None:
        """Run a coroutine, keeping a reference so it is not GC'd mid-flight."""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._on_background_task_done)

    def _on_background_task_done(self, task: asyncio.Task[None]) -> None:
        """Drop the task reference and surface any escaped exception in logs."""
        self._background_tasks.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:
            # Otherwise this only shows up as asyncio's "Task exception was
            # never retrieved" at GC time, which is easy to miss.
            _LOGGER.error("%s: background task failed", self.name, exc_info=exc)

    # -- connection slots --------------------------------------------------
    def _can_connect(self) -> bool:
        """
        Whether a new connection fits within the adapter's slot budget.

        This is an advisory gate, not a reservation: the slot is only recorded
        once the client connects, so two attempts that both pass here before
        either registers can overshoot the budget by one. The bleak slot path
        has the same shape; it bounds, it does not strictly serialize.
        """
        if not self.connectable:
            return False
        slots = self._slot_limit()
        return slots == 0 or len(self._connections) < slots

    def _slot_limit(self) -> int:
        """Configured connection-slot count for this adapter (0 if unregistered)."""
        return get_manager().slot_manager.get_allocations(self.adapter).slots

    def _register_connection(self, address: str) -> None:
        """Record a live connection (called by the client on connect)."""
        self._connections.add(address)

    def _unregister_connection(self, address: str) -> None:
        """Drop a connection (called by the client on disconnect)."""
        self._connections.discard(address)

    def get_allocations(self) -> Allocations | None:
        """Report slot usage from this scanner's own connection tracking."""
        slots = self._slot_limit()
        if not slots:
            return None
        return Allocations(
            self.adapter,
            slots,
            # Clamp at 0: a TOCTOU overshoot past the advisory gate must not
            # report negative free slots.
            max(0, slots - len(self._connections)),
            # Sorted for stable output (the set order is arbitrary), so repeated
            # reads do not look like allocation changes.
            sorted(self._connections),
        )

    # -- discovered-device read-out ---------------------------------------
    # BaseHaScanner leaves these abstract; only remote scanners implement them.
    # They are served from _previous_service_info (populated by the inherited
    # ingestion path) so the manager's unavailable tracking, connect path, and
    # diagnostics work once this scanner is registered.
    @property
    def discovered_devices(self) -> list[BLEDevice]:
        """Return the devices seen so far."""
        return [info.device for info in self._previous_service_info.values()]

    @property
    def discovered_devices_and_advertisement_data(
        self,
    ) -> dict[str, tuple[BLEDevice, AdvertisementData]]:
        """Return each discovered device with its latest advertisement."""
        return {
            address: (info.device, info.advertisement)
            for address, info in self._previous_service_info.items()
        }

    @property
    def discovered_addresses(self) -> Iterable[str]:
        """Return the addresses seen so far."""
        return self._previous_service_info

    def get_discovered_device_advertisement_data(
        self, address: str
    ) -> tuple[BLEDevice, AdvertisementData] | None:
        """Return the device and advertisement for a discovered address."""
        if (info := self._previous_service_info.get(address)) is not None:
            return info.device, info.advertisement
        return None


def create_local_scanner(
    mode: BluetoothScanningMode, adapter: str, address: str
) -> BaseHaScanner:
    """
    Build the best local scanner for an adapter.

    Returns :class:`HaScannerMgmt` when mgmt discovery is available (Linux with a
    usable management socket, an ``hciN`` adapter, and a real adapter BD_ADDR),
    otherwise the bleak-backed :class:`HaScanner`.

    A real ``address`` is required because the L2CAP connect path binds to it to
    pin connections to the adapter that discovered the device; a missing address
    would bind to ``BDADDR_ANY`` and let the kernel pick a different radio.

    AUTO mode falls back to :class:`HaScanner`: the mgmt scanner does not yet
    implement active-window promotion (``async_request_active_window``), so it
    cannot satisfy AUTO's on-demand switch to active scanning.
    """
    manager = get_manager()
    mgmt = manager.get_bluez_mgmt_ctl()
    if (
        IS_LINUX
        and mgmt is not None
        and mgmt.can_discover
        and adapter.startswith("hci")
        and address != DEFAULT_ADDRESS
        and mode is not BluetoothScanningMode.AUTO
    ):
        return HaScannerMgmt(mode, adapter, address)
    return HaScanner(mode, adapter, address)
