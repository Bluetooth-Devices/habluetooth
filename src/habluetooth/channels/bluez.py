from __future__ import annotations

import asyncio
import logging
from asyncio import timeout as asyncio_timeout
from contextlib import asynccontextmanager
from functools import lru_cache
from struct import Struct
from typing import TYPE_CHECKING, Any, cast

from bluetooth_data_tools import monotonic_time_coarse
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

if TYPE_CHECKING:
    import socket
    from collections.abc import AsyncIterator, Callable

    from ..base_scanner import BaseHaScanner

_LOGGER = logging.getLogger(__name__)
_int = int
_bytes = bytes
# Everything is little endian

HEADER_SIZE = 6
# Header is event_code (2 bytes), controller_idx (2 bytes), param_len (2 bytes)
DEVICE_FOUND = 0x0012
ADV_MONITOR_DEVICE_FOUND = 0x002F

# Management commands
MGMT_OP_GET_CONNECTIONS = 0x0015
MGMT_OP_START_DISCOVERY = 0x0023
MGMT_OP_STOP_DISCOVERY = 0x0024
MGMT_OP_LOAD_CONN_PARAM = 0x0035
MGMT_OP_ADD_ADV_PATTERNS_MONITOR = 0x0052
MGMT_OP_REMOVE_ADV_MONITOR = 0x0053

# Management events
MGMT_EV_CMD_COMPLETE = 0x0001
MGMT_EV_CMD_STATUS = 0x0002

# Discovery address-type bitmask: BR/EDR (0x01) | LE Public (0x02) | LE Random (0x04).
# We only scan for LE devices, so we combine the two LE bits.
SCAN_TYPE_LE = 0x06

# Management command status codes
MGMT_STATUS_SUCCESS = 0x00
# Busy means another mgmt client (e.g. bluetoothd) already started discovery;
# device-found events are still delivered to every open socket, so we treat it
# as success for our purposes.
MGMT_STATUS_BUSY = 0x0A

# Pre-compiled struct formats for performance
COMMAND_HEADER = Struct("<HHH")
COMMAND_HEADER_PACK = COMMAND_HEADER.pack
CONN_PARAM_STRUCT = Struct("<H6sBHHHH")
CONN_PARAM_PACK = CONN_PARAM_STRUCT.pack
# START/STOP_DISCOVERY carry a single address-type bitmask byte.
DISCOVERY_STRUCT = Struct("<B")
DISCOVERY_PACK = DISCOVERY_STRUCT.pack
# REMOVE_ADV_MONITOR carries a uint16 monitor handle.
MONITOR_HANDLE_STRUCT = Struct("<H")
MONITOR_HANDLE_PACK = MONITOR_HANDLE_STRUCT.pack
MONITOR_HANDLE_UNPACK = MONITOR_HANDLE_STRUCT.unpack

CONNECTION_ERRORS = (
    BluetoothSocketError,
    OSError,
    asyncio.TimeoutError,
    PermissionError,
)


def _set_future_if_not_done(future: asyncio.Future[None] | None) -> None:
    """Set the future result if not done."""
    if future is not None and not future.done():
        future.set_result(None)


@lru_cache(maxsize=512)
def bytes_mac_to_str(mac: bytes) -> str:
    """Convert a MAC address in bytes to a string in big-endian (MSB-first) order."""
    return ":".join(f"{b:02X}" for b in reversed(mac))


@lru_cache(maxsize=512)
def make_bluez_details(address: str, adapter: str) -> dict[str, Any]:
    """Make the BlueZ details dict for a raw advertisement."""
    base_path = f"/org/bluez/{adapter}"
    return {
        "path": f"{base_path}/dev_{address.replace(':', '_')}",
        "props": {
            "Adapter": base_path,
        },
    }


class BluetoothMGMTProtocol:
    """Bluetooth MGMT protocol."""

    def __init__(
        self,
        connection_made_future: asyncio.Future[None],
        scanners: dict[int, BaseHaScanner],
        on_connection_lost: Callable[[], None],
        is_shutting_down: Callable[[], bool],
        sock: socket.socket,
    ) -> None:
        """Initialize the protocol."""
        self.transport: asyncio.Transport | None = None
        self.connection_made_future = connection_made_future
        self._buffer: bytes | None = None
        self._buffer_len = 0
        self._pos = 0
        self._scanners = scanners
        self._on_connection_lost = on_connection_lost
        self._is_shutting_down = is_shutting_down
        # Keyed by (opcode, controller_idx) so the same command can be in flight
        # on more than one adapter at once without the responses colliding.
        self._pending_commands: dict[
            tuple[int, int], asyncio.Future[tuple[int, bytes]]
        ] = {}
        self._sock = sock

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        """Handle connection made."""
        _set_future_if_not_done(self.connection_made_future)
        self.transport = cast("asyncio.Transport", transport)

    def _write_to_socket(self, data: bytes) -> None:
        """
        Write data directly to the socket, bypassing asyncio transport.

        This works around a kernel bug where sendto() on Bluetooth management
        sockets returns 0 instead of the number of bytes sent on some platforms
        (e.g., Odroid M1 with kernel 6.12.43). When asyncio sees 0, it thinks
        the send failed and retries forever.

        Since mgmt sockets are SOCK_RAW, sends are atomic - either the entire
        packet is sent or nothing is sent.
        """
        try:
            n = self._sock.send(data)
            # On buggy kernels, n might be 0 even though the data was sent
            # We treat 0 as success for mgmt sockets
            if n == 0 and len(data) > 0:
                # Kernel bug: returned 0 but data was actually sent
                _LOGGER.debug(
                    "Bluetooth mgmt socket returned 0 for %d bytes (kernel bug fix)",
                    len(data),
                )
        except Exception:
            _LOGGER.exception("Failed to write to mgmt socket")
            raise

    @asynccontextmanager
    async def command_response(
        self, opcode: int, controller_idx: int
    ) -> AsyncIterator[asyncio.Future[tuple[int, bytes]]]:
        """
        Context manager for handling command responses.

        Only one command per (opcode, controller_idx) may be in flight at a
        time: a second concurrent call with the same key overwrites the first
        waiter, which would then never resolve. The mgmt command wrappers never
        double-issue the same command on one adapter, so this is sufficient.

        Usage:
            async with protocol.command_response(opcode, controller_idx) as future:
                transport.write(command)
                status, data = await future
        """
        future: asyncio.Future[tuple[int, bytes]] = (
            asyncio.get_running_loop().create_future()
        )
        key = (opcode, controller_idx)
        self._pending_commands[key] = future
        try:
            yield future
        finally:
            # Clean up if the future wasn't resolved
            self._pending_commands.pop(key, None)

    def _add_to_buffer(self, data: bytes | bytearray | memoryview) -> None:
        """Add data to the buffer."""
        # Protractor sends a bytearray, so we need to convert it to bytes
        # https://github.com/esphome/issues/issues/5117
        # type(data) should not be isinstance(data, bytes) because we want to
        # to explicitly check for bytes and not for subclasses of bytes
        bytes_data = bytes(data) if type(data) is not bytes else data
        if self._buffer_len == 0:
            # This is the best case scenario, we don't have to copy the data
            # and can just use the buffer directly. This is the most common
            # case as well.
            self._buffer = bytes_data
        else:
            if TYPE_CHECKING:
                assert self._buffer is not None, "Buffer should be set"
            # This is the worst case scenario, we have to copy the bytes_data
            # and can't just use the buffer directly. This is also very
            # uncommon since we usually read the entire frame at once.
            self._buffer += bytes_data
        self._buffer_len += len(bytes_data)

    def _remove_from_buffer(self) -> None:
        """Remove data from the buffer."""
        end_of_frame_pos = self._pos
        self._buffer_len -= end_of_frame_pos
        if self._buffer_len == 0:
            # This is the best case scenario, we can just set the buffer to None
            # and don't have to copy the data. This is the most common case as well.
            self._buffer = None
            return
        if TYPE_CHECKING:
            assert self._buffer is not None, "Buffer should be set"
        # This is the worst case scenario, we have to copy the data
        # and can't just use the buffer directly. This should only happen
        # when we read multiple frames at once because the event loop
        # is blocked and we cannot pull the data out of the buffer fast enough.
        cstr = self._buffer
        # Important: we must use the explicit length for the slice
        # since Cython will stop at any '\0' character if we don't
        self._buffer = cstr[end_of_frame_pos : self._buffer_len + end_of_frame_pos]

    def data_received(self, data: _bytes) -> None:  # noqa: C901
        """Handle data received."""
        self._add_to_buffer(data)
        while self._buffer_len >= 6:
            if TYPE_CHECKING:
                assert self._buffer is not None, "Buffer should be set"
            self._pos = 6
            header = self._buffer
            event_code = header[0] | (header[1] << 8)
            controller_idx = header[2] | (header[3] << 8)
            param_len = header[4] | (header[5] << 8)
            if self._buffer_len < self._pos + param_len:
                # We don't have the entire frame yet, so we need to wait
                # for more data to arrive.
                return
            self._pos += param_len
            if event_code == DEVICE_FOUND:
                parse_offset = 6
            elif event_code == ADV_MONITOR_DEVICE_FOUND:
                parse_offset = 8
            elif event_code in {MGMT_EV_CMD_COMPLETE, MGMT_EV_CMD_STATUS}:
                # Handle management command responses
                if param_len >= 3:
                    opcode = header[6] | (header[7] << 8)
                    status = header[8]
                    if opcode == MGMT_OP_LOAD_CONN_PARAM:
                        # Fire-and-forget command, no waiter to resolve.
                        self._handle_load_conn_param_response(status, controller_idx)
                    elif (
                        future := self._pending_commands.get((opcode, controller_idx))
                    ) is not None:
                        # Resolve whatever awaited this (opcode, controller_idx):
                        # capability check, discovery, advertisement monitor, etc.
                        del self._pending_commands[(opcode, controller_idx)]
                        if not future.done():
                            response_data = (
                                header[9 : self._pos] if param_len > 3 else b""
                            )
                            future.set_result((status, response_data))
                self._remove_from_buffer()
                continue
            else:
                self._remove_from_buffer()
                continue
            address = header[parse_offset : parse_offset + 6]
            rssi = header[parse_offset + 7]
            if rssi > 128:
                rssi -= 256

            # Skip address_type (+6), flags (+8..+11) and AD_Data_Length
            # (+12..+13); the advertising data payload starts at +14.
            data = header[parse_offset + 14 : self._pos]
            self._remove_from_buffer()

            if (scanner := self._scanners.get(controller_idx)) is not None:
                # Adapt the raw BlueZ advertisement onto the generic ingestion
                # path. The BlueZ-specific bits (MAC byte order, object path)
                # live here in the channel rather than on the scanner classes,
                # which only expose the platform-agnostic _async_on_raw_advertisement.
                address_str = bytes_mac_to_str(address)
                scanner._async_on_raw_advertisement(
                    address_str,
                    rssi,
                    data,
                    make_bluez_details(address_str, scanner.adapter),
                    monotonic_time_coarse(),
                )

    def _handle_load_conn_param_response(
        self, status: _int, controller_idx: _int
    ) -> None:
        """Handle MGMT_OP_LOAD_CONN_PARAM response."""
        if status != 0:
            _LOGGER.warning(
                "hci%u: Failed to load conn params: status=%d",
                controller_idx,
                status,
            )
        else:
            _LOGGER.debug(
                "hci%u: Connection parameters loaded successfully",
                controller_idx,
            )

    def connection_lost(self, exc: Exception | None) -> None:
        """Handle connection lost."""
        # Only suppress warnings during shutdown, not info messages
        if exc:
            if not self._is_shutting_down():
                _LOGGER.warning("Bluetooth management socket connection lost: %s", exc)
        else:
            _LOGGER.info("Bluetooth management socket connection closed")
        self.transport = None
        self._on_connection_lost()


class MGMTBluetoothCtl:
    """Class to control interfaces using the BlueZ management API."""

    def __init__(self, timeout: float, scanners: dict[int, BaseHaScanner]) -> None:
        """Initialize the control class."""
        # Internal state
        self.timeout = timeout
        self.protocol: BluetoothMGMTProtocol | None = None
        self.sock: socket.socket | None = None
        self.scanners = scanners
        self._reconnect_task: asyncio.Task[None] | None = None
        self._on_connection_lost_future: asyncio.Future[None] | None = None
        self._shutting_down = False
        # Set once setup() confirms NET_ADMIN/NET_RAW; the same privileges that
        # allow the capability probe allow driving discovery over the socket.
        self.can_discover = False

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
                        self.sock,
                    ),
                    None,
                    None,
                )
                await connection_made_future
        except TimeoutError:
            btmgmt_socket.close(self.sock)
            raise
        _LOGGER.debug("Bluetooth management socket connection established")
        self.protocol = cast("BluetoothMGMTProtocol", protocol)
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
                "MGMT capability check failed: %s - likely missing NET_ADMIN/NET_RAW",
                ex,
            )
            return False

    async def _do_mgmt_op_get_connections(self, header: bytes) -> bool:
        """Send a MGMT_OP_GET_CONNECTIONS command and check capabilities."""
        if TYPE_CHECKING:
            assert self.protocol is not None
            assert self.protocol.transport is not None

        async with self.protocol.command_response(
            MGMT_OP_GET_CONNECTIONS, 0
        ) as response_future:
            self.protocol._write_to_socket(header)
            # Wait for response with timeout
            async with asyncio_timeout(5.0):
                status, _ = await response_future
            return self._has_mgmt_capabilities_from_status(status)

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
            msg = "Missing NET_ADMIN/NET_RAW capabilities for Bluetooth management"
            raise PermissionError(msg)

        self.can_discover = True
        self._reconnect_task = asyncio.create_task(self.reconnect_task())

    async def _send_command_await(
        self, opcode: int, adapter_idx: int, cmd_data: bytes
    ) -> tuple[int, bytes] | None:
        """
        Send a mgmt command and await its command-complete/status response.

        Returns ``(status, response_params)``, or ``None`` if the socket is
        down, the write fails, or no response arrives within the timeout.
        """
        if not self.protocol or not self.protocol.transport:
            _LOGGER.error("Cannot send mgmt command %#x: no connection", opcode)
            return None
        header = COMMAND_HEADER_PACK(opcode, adapter_idx, len(cmd_data))
        try:
            async with self.protocol.command_response(opcode, adapter_idx) as future:
                # _write_to_socket can raise OSError or BluetoothSocketError when
                # the socket has gone away; treat any of these as a soft failure.
                self.protocol._write_to_socket(header + cmd_data)
                async with asyncio_timeout(self.timeout):
                    return await future
        except (TimeoutError, OSError, BluetoothSocketError) as ex:
            _LOGGER.debug("hci%u: mgmt command %#x failed: %s", adapter_idx, opcode, ex)
            return None

    async def start_discovery(self, adapter_idx: int) -> bool:
        """
        Start LE discovery on an adapter over the management socket.

        Returns True if discovery is running, including the case where another
        mgmt client (e.g. bluetoothd) already started it (BUSY); device-found
        events are delivered to every open socket regardless of who started
        discovery. A failure here means the adapter produces no advertisements,
        so it is logged at warning level.
        """
        result = await self._send_command_await(
            MGMT_OP_START_DISCOVERY, adapter_idx, DISCOVERY_PACK(SCAN_TYPE_LE)
        )
        if result is not None and result[0] in (
            MGMT_STATUS_SUCCESS,
            MGMT_STATUS_BUSY,
        ):
            return True
        _LOGGER.warning(
            "hci%u: failed to start discovery: %s",
            adapter_idx,
            "no response" if result is None else f"status={result[0]:#x}",
        )
        return False

    async def stop_discovery(self, adapter_idx: int) -> bool:
        """
        Stop LE discovery on an adapter over the management socket.

        Only MGMT_STATUS_SUCCESS means discovery actually stopped. Unlike start,
        a BUSY status here means the kernel did NOT stop discovery (e.g. another
        mgmt client is still discovering), so it is not treated as success.
        """
        result = await self._send_command_await(
            MGMT_OP_STOP_DISCOVERY, adapter_idx, DISCOVERY_PACK(SCAN_TYPE_LE)
        )
        if result is not None and result[0] == MGMT_STATUS_SUCCESS:
            return True
        # Best-effort cleanup on a shared socket, so this stays at debug, but
        # log the no-response case too for parity with the other wrappers.
        _LOGGER.debug(
            "hci%u: stop discovery did not stop: %s",
            adapter_idx,
            "no response" if result is None else f"status={result[0]:#x}",
        )
        return False

    async def add_adv_pattern_monitor(self, adapter_idx: int) -> int | None:
        """
        Register a match-all advertisement monitor for passive scanning.

        A pattern_count of 0 tells the controller to report every
        advertisement via ADV_MONITOR_DEVICE_FOUND. Returns the assigned
        monitor handle (for later removal), or None on failure.
        """
        result = await self._send_command_await(
            MGMT_OP_ADD_ADV_PATTERNS_MONITOR, adapter_idx, b"\x00"
        )
        if result is None:
            return None
        status, data = result
        if status == MGMT_STATUS_SUCCESS:
            if len(data) >= 2:
                return MONITOR_HANDLE_UNPACK(data[:2])[0]
            # Success without the 2-byte handle: the monitor may be registered
            # in the controller but we cannot track it for later removal.
            _LOGGER.warning(
                "hci%u: add advertisement monitor succeeded without a handle",
                adapter_idx,
            )
            return None
        _LOGGER.warning(
            "hci%u: failed to add advertisement monitor: status=%#x",
            adapter_idx,
            status,
        )
        return None

    async def remove_adv_monitor(self, adapter_idx: int, handle: int) -> bool:
        """Remove a previously registered advertisement monitor by handle."""
        result = await self._send_command_await(
            MGMT_OP_REMOVE_ADV_MONITOR, adapter_idx, MONITOR_HANDLE_PACK(handle)
        )
        if result is not None and result[0] == MGMT_STATUS_SUCCESS:
            return True
        # A failed removal leaves the monitor registered controller-side, so
        # surface it at warning level to make a leaked handle diagnosable.
        _LOGGER.warning(
            "hci%u: failed to remove advertisement monitor %u: %s",
            adapter_idx,
            handle,
            "no response" if result is None else f"status={result[0]:#x}",
        )
        return False

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

        # Build command structure (C definitions from BlueZ mgmt-api.txt):
        # struct mgmt_cp_load_conn_param {
        #     uint16_t param_count;
        #     struct mgmt_conn_param params[0];
        # };
        # struct mgmt_conn_param {
        #     struct mgmt_addr_info addr;
        #     uint16_t min_interval;
        #     uint16_t max_interval;
        #     uint16_t latency;
        #     uint16_t timeout;
        # };
        # struct mgmt_addr_info {
        #     bdaddr_t bdaddr;
        #     uint8_t type;
        # };

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
            self.protocol._write_to_socket(header + cmd_data)
            _LOGGER.debug(
                "Loaded conn params for %s: interval=%d-%d, latency=%d, timeout=%d",
                address,
                min_interval,
                max_interval,
                latency,
                timeout,
            )
        except Exception:
            _LOGGER.exception("Failed to load conn params")
            return False
        else:
            return True

    def load_conn_params_explicit(
        self,
        adapter_idx: int,
        address: str,
        address_type: int,
        min_interval: int,
        max_interval: int,
        latency: int,
        timeout: int,
    ) -> bool:
        """
        Load explicit connection parameters for a specific device.

        Args:
            adapter_idx: Adapter index (e.g., 0 for hci0)
            address: Device MAC address (e.g., "AA:BB:CC:DD:EE:FF")
            address_type: BDADDR_LE_PUBLIC (1) or BDADDR_LE_RANDOM (2)
            min_interval: Minimum connection interval (units of 1.25ms)
            max_interval: Maximum connection interval (units of 1.25ms)
            latency: Connection latency (number of events)
            timeout: Supervision timeout (units of 10ms)

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

        # Pack the command
        cmd_data = CONN_PARAM_PACK(
            1,  # param_count = 1
            addr_bytes[::-1],  # bdaddr (reversed for little endian)
            address_type,  # address type
            min_interval,
            max_interval,
            latency,
            timeout,
        )

        # Send the command
        try:
            header = COMMAND_HEADER_PACK(
                MGMT_OP_LOAD_CONN_PARAM,  # opcode
                adapter_idx,  # controller index
                len(cmd_data),  # parameter length
            )
            self.protocol._write_to_socket(header + cmd_data)
            _LOGGER.debug(
                "Loaded explicit conn params for %s:"
                " interval=%d-%d, latency=%d, timeout=%d",
                address,
                min_interval,
                max_interval,
                latency,
                timeout,
            )
        except Exception:
            _LOGGER.exception("Failed to load conn params")
            return False
        else:
            return True
