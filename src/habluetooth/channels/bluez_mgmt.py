"""Bluetooth management interface using BlueZ management API."""

from __future__ import annotations

import asyncio
import logging
import socket
from asyncio import timeout as asyncio_timeout
from struct import Struct
from typing import TYPE_CHECKING, cast

from btsocket import btmgmt_socket
from btsocket.btmgmt_socket import BluetoothSocketError

from ..const import (
    FAST_CONN_LATENCY,
    FAST_CONN_TIMEOUT,
    FAST_MAX_CONN_INTERVAL,
    FAST_MIN_CONN_INTERVAL,
    MEDIUM_CONN_LATENCY,
    MEDIUM_CONN_TIMEOUT,
    MEDIUM_MAX_CONN_INTERVAL,
    MEDIUM_MIN_CONN_INTERVAL,
    ConnectParams,
)
from ..scanner import HaScanner
from .bluez import BluetoothMGMTProtocol

# Management commands
MGMT_OP_GET_CONNECTIONS = 0x0015
MGMT_OP_LOAD_CONN_PARAM = 0x0035

# Pre-compiled struct formats for performance
COMMAND_HEADER = Struct("<HHH")
COMMAND_HEADER_PACK = COMMAND_HEADER.pack
CONN_PARAM_STRUCT = Struct("<H6sBHHHH")
CONN_PARAM_PACK = CONN_PARAM_STRUCT.pack


def _set_future_if_not_done(future: asyncio.Future[None] | None) -> None:
    """Set the future result if not done."""
    if future is not None and not future.done():
        future.set_result(None)


_LOGGER = logging.getLogger(__name__)

CONNECTION_ERRORS = (
    BluetoothSocketError,
    OSError,
    asyncio.TimeoutError,
    PermissionError,
)


class MGMTBluetoothCtl:
    """Class to control interfaces using the BlueZ management API."""

    def __init__(self, timeout: float, scanners: dict[int, HaScanner]) -> None:
        """Initialize the control class."""
        # Internal state
        self.timeout = timeout
        self.protocol: BluetoothMGMTProtocol | None = None
        self.sock: socket.socket | None = None
        self.scanners = scanners
        self._reconnect_task: asyncio.Task[None] | None = None
        self._on_connection_lost_future: asyncio.Future[None] | None = None
        self._shutting_down = False

    def close(self) -> None:
        """Close the management interface."""
        self._shutting_down = True
        if self._reconnect_task:
            self._reconnect_task.cancel()
        if self.protocol and self.protocol.transport:
            self.protocol.transport.close()
            self.protocol = None
        btmgmt_socket.close(self.sock)

    def _on_connection_lost(self) -> None:
        """Handle connection lost."""
        if self._shutting_down:
            _LOGGER.debug("Bluetooth management socket connection lost during shutdown")
        else:
            _LOGGER.debug("Bluetooth management socket connection lost, reconnecting")
        _set_future_if_not_done(self._on_connection_lost_future)
        self._on_connection_lost_future = None

    async def reconnect_task(self) -> None:
        """Monitor the connection and reconnect if needed."""
        while not self._shutting_down:
            if self._on_connection_lost_future:
                await self._on_connection_lost_future
            if self._shutting_down:
                break  # type: ignore[unreachable]
            _LOGGER.debug("Reconnecting to Bluetooth management socket")
            try:
                await self._establish_connection()
            except CONNECTION_ERRORS:
                _LOGGER.debug("Bluetooth management socket connection timed out")
                # If we get a timeout, we should try to reconnect
                # after a short delay
                await asyncio.sleep(1)

    async def _establish_connection(self) -> None:
        """Establish a connection to the Bluetooth management socket."""
        _LOGGER.debug("Establishing Bluetooth management socket connection")
        self.sock = btmgmt_socket.open()
        loop = asyncio.get_running_loop()
        connection_made_future: asyncio.Future[None] = loop.create_future()
        try:
            async with asyncio_timeout(self.timeout):
                # _create_connection_transport accessed
                # directly to avoid SOCK_STREAM check
                # see https://bugs.python.org/issue38285
                _, protocol = await loop._create_connection_transport(  # type: ignore[attr-defined]
                    self.sock,
                    lambda: BluetoothMGMTProtocol(
                        connection_made_future,
                        self.scanners,
                        self._on_connection_lost,
                        lambda: self._shutting_down,
                    ),
                    None,
                    None,
                )
                await connection_made_future
        except TimeoutError:
            btmgmt_socket.close(self.sock)
            raise
        _LOGGER.debug("Bluetooth management socket connection established")
        self.protocol = cast(BluetoothMGMTProtocol, protocol)
        self._on_connection_lost_future = loop.create_future()

    def _has_mgmt_capabilities_from_status(self, status: int) -> bool:
        """
        Check if a MGMT command status indicates we have capabilities.

        Returns True if we have capabilities, False otherwise.

        Status codes:
        - 0x00 = Success (we have permissions)
        - 0x01 = Unknown Command (might happen if kernel is too old)
        - 0x0D = Invalid Parameters
        - 0x10 = Not Powered (for some operations)
        - 0x11 = Invalid Index (adapter doesn't exist but we have permissions)
        - 0x14 = Permission Denied (missing NET_ADMIN/NET_RAW)
        """
        if status == 0x14:  # Permission denied
            _LOGGER.debug(
                "MGMT capability check failed with permission denied - "
                "missing NET_ADMIN/NET_RAW"
            )
            return False
        if status in (0x00, 0x11):  # Success or Invalid Index
            _LOGGER.debug("MGMT capability check passed (status: %#x)", status)
            return True
        # Unknown status - log it and assume no permissions to be safe
        _LOGGER.debug(
            "MGMT capability check returned unexpected status %#x - "
            "assuming missing permissions",
            status,
        )
        return False

    async def _check_capabilities(self) -> bool:
        """
        Check if we have the necessary capabilities to use MGMT.

        Returns True if we have capabilities, False otherwise.
        """
        if not self.protocol or not self.protocol.transport:
            return False

        # Try GET_CONNECTIONS for adapter 0 - this is a read-only command
        # that requires NET_ADMIN privileges but doesn't change any state
        header = COMMAND_HEADER_PACK(
            MGMT_OP_GET_CONNECTIONS,  # opcode
            0,  # controller index 0 (hci0)
            0,  # no parameters
        )

        try:
            return await self._do_mgmt_op_get_connections(header)
        except (TimeoutError, OSError) as ex:
            _LOGGER.debug(
                "MGMT capability check failed: %s - "
                "likely missing NET_ADMIN/NET_RAW",
                ex,
            )
            return False

    async def _do_mgmt_op_get_connections(self, header: bytes) -> bool:
        """Send a MGMT_OP_GET_CONNECTIONS command and check capabilities."""
        if TYPE_CHECKING:
            assert self.protocol is not None
            assert self.protocol.transport is not None

        response_future = self.protocol.setup_command_response(MGMT_OP_GET_CONNECTIONS)
        try:
            self.protocol.transport.write(header)
            # Wait for response with timeout
            async with asyncio_timeout(5.0):
                status, _ = await response_future
            return self._has_mgmt_capabilities_from_status(status)
        finally:
            self.protocol.cleanup_command_response(MGMT_OP_GET_CONNECTIONS)

    async def setup(self) -> None:
        """Set up management interface."""
        await self._establish_connection()

        # Check if we actually have the capabilities to use MGMT
        if not await self._check_capabilities():
            # Mark as shutting down to prevent reconnection attempts
            self._shutting_down = True
            # Close the connection and raise an error to trigger fallback
            if self.protocol and self.protocol.transport:
                self.protocol.transport.close()
            btmgmt_socket.close(self.sock)
            raise PermissionError(
                "Missing NET_ADMIN/NET_RAW capabilities for Bluetooth management"
            )

        self._reconnect_task = asyncio.create_task(self.reconnect_task())

    def load_conn_params(
        self,
        adapter_idx: int,
        address: str,
        address_type: int,
        params: ConnectParams,
    ) -> bool:
        """
        Load connection parameters for a specific device.

        Args:
            adapter_idx: Adapter index (e.g., 0 for hci0)
            address: Device MAC address (e.g., "AA:BB:CC:DD:EE:FF")
            address_type: BDADDR_LE_PUBLIC (1) or BDADDR_LE_RANDOM (2)
            params: Connection parameters to load (ConnectParams.FAST or
              ConnectParams.MEDIUM)

        Returns:
            True if command was sent successfully

        """
        if not self.protocol or not self.protocol.transport:
            _LOGGER.error("Cannot load conn params: no connection")
            return False

        # Parse MAC address
        addr_bytes = bytes.fromhex(address.replace(":", ""))
        if len(addr_bytes) != 6:
            _LOGGER.error("Invalid MAC address: %s", address)
            return False

        # Build command structure
        # struct mgmt_cp_load_conn_param {
        #     uint16_t param_count;
        #     struct mgmt_conn_param params[0];
        # }
        # struct mgmt_conn_param {
        #     struct mgmt_addr_info addr;
        #     uint16_t min_interval;
        #     uint16_t max_interval;
        #     uint16_t latency;
        #     uint16_t timeout;
        # }
        # struct mgmt_addr_info {
        #     bdaddr_t bdaddr;
        #     uint8_t type;
        # }

        # Get the appropriate connection parameters based on the enum
        if params is ConnectParams.FAST:
            min_interval = FAST_MIN_CONN_INTERVAL
            max_interval = FAST_MAX_CONN_INTERVAL
            latency = FAST_CONN_LATENCY
            timeout = FAST_CONN_TIMEOUT
        else:  # params is ConnectParams.MEDIUM:
            min_interval = MEDIUM_MIN_CONN_INTERVAL
            max_interval = MEDIUM_MAX_CONN_INTERVAL
            latency = MEDIUM_CONN_LATENCY
            timeout = MEDIUM_CONN_TIMEOUT

        # Pack the command
        cmd_data = CONN_PARAM_PACK(
            1,  # param_count = 1
            addr_bytes[::-1],  # bdaddr (reversed for little endian)
            address_type,  # address type
            min_interval,  # min_interval
            max_interval,  # max_interval
            latency,  # latency
            timeout,  # timeout
        )

        # Send the command
        try:
            header = COMMAND_HEADER_PACK(
                MGMT_OP_LOAD_CONN_PARAM,  # opcode
                adapter_idx,  # controller index
                len(cmd_data),  # parameter length
            )
            self.protocol.transport.write(header + cmd_data)
            _LOGGER.debug(
                "Loaded conn params for %s: interval=%d-%d, latency=%d, timeout=%d",
                address,
                min_interval,
                max_interval,
                latency,
                timeout,
            )
            return True
        except Exception:
            _LOGGER.exception("Failed to load conn params")
            return False
