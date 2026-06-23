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
from .const import SOURCE_LOCAL
from .models import BluetoothScanningMode, HaBluetoothConnector
from .scanner_bleak import IS_LINUX, HaScanner, ScannerStartError, _resolve_radio_mode

if TYPE_CHECKING:
    from collections.abc import Coroutine
    from typing import Any

    from bleak.backends.client import BaseBleakClient

    from .const import CALLBACK_TYPE

_LOGGER = logging.getLogger(__name__)


class HaScannerMgmt(BaseHaScanner):
    """A local scanner that discovers and connects over the BlueZ mgmt socket."""

    def __init__(self, mode: BluetoothScanningMode, adapter: str, address: str) -> None:
        """Set up the scanner and wire connections to the mgmt GATT client."""
        self.mac_address = address
        source = address if address != DEFAULT_ADDRESS else adapter or SOURCE_LOCAL
        connector = HaBluetoothConnector(
            # partial-bind the per-connection data; the wrapper calls
            # connector.client(device, ...) like any backend class.
            client=cast(
                "type[BaseBleakClient]",
                partial(
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
        self.current_mode = mode
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
            return
        if self._monitor_handle is not None:
            await mgmt.remove_adv_monitor(idx, self._monitor_handle)
            self._monitor_handle = None
        else:
            await mgmt.stop_discovery(idx)

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
        task.add_done_callback(self._background_tasks.discard)

    # -- connection slots --------------------------------------------------
    def _can_connect(self) -> bool:
        """Whether a new connection fits within the adapter's slot budget."""
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
            slots - len(self._connections),
            list(self._connections),
        )


def create_local_scanner(
    mode: BluetoothScanningMode, adapter: str, address: str
) -> BaseHaScanner:
    """
    Build the best local scanner for an adapter.

    Returns :class:`HaScannerMgmt` when mgmt discovery is available (Linux with a
    usable management socket and an ``hciN`` adapter), otherwise the bleak-backed
    :class:`HaScanner`.
    """
    manager = get_manager()
    mgmt = manager.get_bluez_mgmt_ctl()
    if (
        IS_LINUX
        and mgmt is not None
        and mgmt.can_discover
        and adapter.startswith("hci")
    ):
        return HaScannerMgmt(mode, adapter, address)
    return HaScanner(mode, adapter, address)
