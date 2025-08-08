from __future__ import annotations

import asyncio
import logging
import socket
from asyncio import timeout as asyncio_timeout
from collections.abc import Callable
from struct import Struct
from typing import TYPE_CHECKING, cast

from btsocket import btmgmt_protocol, btmgmt_socket
from btsocket.btmgmt_socket import BluetoothSocketError

from ..const import (
    BDADDR_LE_PUBLIC,
    FAST_CONN_LATENCY,
    FAST_CONN_TIMEOUT,
    FAST_MAX_CONN_INTERVAL,
    FAST_MIN_CONN_INTERVAL,
    MEDIUM_CONN_LATENCY,
    MEDIUM_CONN_TIMEOUT,
    MEDIUM_MAX_CONN_INTERVAL,
    MEDIUM_MIN_CONN_INTERVAL,
)
from ..scanner import HaScanner

_LOGGER = logging.getLogger(__name__)
_int = int
_bytes = bytes
# Everything is little endian

HEADER_SIZE = 6
# Header is event_code (2 bytes), controller_idx (2 bytes), param_len (2 bytes)
DEVICE_FOUND = 0x0012
ADV_MONITOR_DEVICE_FOUND = 0x002F

# Management commands
MGMT_OP_LOAD_CONN_PARAM = 0x0035

# Management events
MGMT_EV_CMD_COMPLETE = 0x0001
MGMT_EV_CMD_STATUS = 0x0002

# Pre-compiled struct formats for performance
COMMAND_HEADER = Struct("<HHH")
COMMAND_HEADER_PACK = COMMAND_HEADER.pack
CONN_PARAM_STRUCT = Struct("<H6sBHHHH")
CONN_PARAM_PACK = CONN_PARAM_STRUCT.pack

CONNECTION_ERRORS = (
    BluetoothSocketError,
    OSError,
    asyncio.TimeoutError,
    PermissionError,
)


class BluetoothMGMTProtocol:
    """Bluetooth MGMT protocol."""

    def __init__(
        self,
        connection_mode_future: asyncio.Future[None],
        scanners: dict[int, HaScanner],
        on_connection_lost: Callable[[], None],
    ) -> None:
        """Initialize the protocol."""
        self.transport: asyncio.Transport | None = None
        self.connection_mode_future = connection_mode_future
        self._buffer: bytes | None = None
        self._buffer_len = 0
        self._pos = 0
        self._scanners = scanners
        self._on_connection_lost = on_connection_lost

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        """Handle connection made."""
        if not self.connection_mode_future.done():
            self.connection_mode_future.set_result(None)
        self.transport = cast(asyncio.Transport, transport)

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

    def _read(self, length: _int) -> bytes | None:
        """Read exactly length bytes from the buffer or None if all the bytes are not yet available."""  # noqa: E501
        new_pos = self._pos + length
        if self._buffer_len < new_pos:
            return None
        original_pos = self._pos
        self._pos = new_pos
        if TYPE_CHECKING:
            assert self._buffer is not None, "Buffer should be set"
        cstr = self._buffer
        # Important: we must keep the bounds check (self._buffer_len < new_pos)
        # above to verify we never try to read past the end of the buffer
        return cstr[original_pos:new_pos]

    def data_received(self, data: _bytes) -> None:
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
            elif event_code == MGMT_EV_CMD_COMPLETE or event_code == MGMT_EV_CMD_STATUS:
                # Handle management command responses
                if param_len >= 3:
                    opcode = header[6] | (header[7] << 8)
                    status = header[8]
                    if opcode == MGMT_OP_LOAD_CONN_PARAM:
                        if status == 0:
                            _LOGGER.debug(
                                "hci%u: Connection parameters loaded successfully",
                                controller_idx,
                            )
                        else:
                            _LOGGER.warning(
                                "hci%u: Failed to load conn params: status=%d",
                                controller_idx,
                                status,
                            )
                self._remove_from_buffer()
                continue
            else:
                self._remove_from_buffer()
                continue
            address = header[parse_offset : parse_offset + 6]
            address_type = header[parse_offset + 6]
            rssi = header[parse_offset + 7]
            if rssi > 128:
                rssi -= 256

            flags = (
                header[parse_offset + 8]
                | (header[parse_offset + 9] << 8)
                | (header[parse_offset + 10] << 16)
                | (header[parse_offset + 11] << 24)
            )

            # Skip AD_Data_Length (2 bytes) at parse_offset+12 and +13
            data = header[parse_offset + 14 : self._pos]
            self._remove_from_buffer()

            if (scanner := self._scanners.get(controller_idx)) is not None:
                # We have a scanner for this controller, so we can
                # pass the data to it.
                scanner._async_on_raw_bluez_advertisement(
                    address,
                    address_type,
                    rssi,
                    flags,
                    data,
                )

    def _timeout_future(self, future: asyncio.Future[btmgmt_protocol.Response]) -> None:
        if future and not future.done():
            future.set_exception(TimeoutError("Timeout waiting for response"))

    def connection_lost(self, exc: Exception | None) -> None:
        """Handle connection lost."""
        if exc:
            _LOGGER.warning("Bluetooth management socket connection lost: %s", exc)
        else:
            _LOGGER.info("Bluetooth management socket connection closed")
        self.transport = None
        self._on_connection_lost()


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

    def close(self) -> None:
        """Close the management interface."""
        if self._reconnect_task:
            self._reconnect_task.cancel()
        if self.protocol and self.protocol.transport:
            self.protocol.transport.close()
            self.protocol = None
        btmgmt_socket.close(self.sock)

    def _on_connection_lost(self) -> None:
        """Handle connection lost."""
        _LOGGER.debug("Bluetooth management socket connection lost, reconnecting")
        if (
            self._on_connection_lost_future
            and not self._on_connection_lost_future.done()
        ):
            self._on_connection_lost_future.set_result(None)
            self._on_connection_lost_future = None

    async def reconnect_task(self) -> None:
        """Monitor the connection and reconnect if needed."""
        while True:
            if self._on_connection_lost_future:
                await self._on_connection_lost_future
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
                        connection_made_future, self.scanners, self._on_connection_lost
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

    async def setup(self) -> None:
        """Set up management interface."""
        await self._establish_connection()
        self._reconnect_task = asyncio.create_task(self.reconnect_task())

    def load_conn_params(
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
        Load connection parameters for a specific device.

        Args:
            adapter_idx: Adapter index (e.g., 0 for hci0)
            address: Device MAC address (e.g., "AA:BB:CC:DD:EE:FF")
            address_type: BDADDR_LE_PUBLIC (1) or BDADDR_LE_RANDOM (2)
            min_interval: Min connection interval (units of 1.25ms)
            max_interval: Max connection interval (units of 1.25ms)
            latency: Slave latency (number of events)
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
        except Exception as e:
            _LOGGER.error("Failed to load conn params: %s", e)
            return False

    def load_fast_conn_params(
        self, adapter_idx: int, address: str, address_type: int = BDADDR_LE_PUBLIC
    ) -> bool:
        """Load fast connection parameters for initial connection/service discovery."""
        return self.load_conn_params(
            adapter_idx,
            address,
            address_type,
            FAST_MIN_CONN_INTERVAL,
            FAST_MAX_CONN_INTERVAL,
            FAST_CONN_LATENCY,
            FAST_CONN_TIMEOUT,
        )

    def load_medium_conn_params(
        self, adapter_idx: int, address: str, address_type: int = BDADDR_LE_PUBLIC
    ) -> bool:
        """Load medium connection parameters for standard operation."""
        return self.load_conn_params(
            adapter_idx,
            address,
            address_type,
            MEDIUM_MIN_CONN_INTERVAL,
            MEDIUM_MAX_CONN_INTERVAL,
            MEDIUM_CONN_LATENCY,
            MEDIUM_CONN_TIMEOUT,
        )
