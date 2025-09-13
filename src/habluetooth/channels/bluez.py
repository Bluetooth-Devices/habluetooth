from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from struct import Struct
from typing import TYPE_CHECKING, cast

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
MGMT_OP_GET_CONNECTIONS = 0x0015
MGMT_OP_LOAD_CONN_PARAM = 0x0035

# Management events
MGMT_EV_CMD_COMPLETE = 0x0001
MGMT_EV_CMD_STATUS = 0x0002

# Pre-compiled struct formats for performance
COMMAND_HEADER = Struct("<HHH")
COMMAND_HEADER_PACK = COMMAND_HEADER.pack
CONN_PARAM_STRUCT = Struct("<H6sBHHHH")
CONN_PARAM_PACK = CONN_PARAM_STRUCT.pack


def _set_future_if_not_done(future: asyncio.Future[None] | None) -> None:
    """Set the future result if not done."""
    if future is not None and not future.done():
        future.set_result(None)


class BluetoothMGMTProtocol:
    """Bluetooth MGMT protocol."""

    def __init__(
        self,
        connection_made_future: asyncio.Future[None],
        scanners: dict[int, HaScanner],
        on_connection_lost: Callable[[], None],
        is_shutting_down: Callable[[], bool],
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
        self._pending_commands: dict[int, asyncio.Future[tuple[int, bytes]]] = {}

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        """Handle connection made."""
        _set_future_if_not_done(self.connection_made_future)
        self.transport = cast(asyncio.Transport, transport)

    def setup_command_response(self, opcode: int) -> asyncio.Future[tuple[int, bytes]]:
        """
        Set up a future for handling command responses.

        Usage:
            future = protocol.setup_command_response(opcode)
            transport.write(command)
            try:
                status, data = await future
            finally:
                protocol.cleanup_command_response(opcode)
        """
        future: asyncio.Future[tuple[int, bytes]] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending_commands[opcode] = future
        return future

    def cleanup_command_response(self, opcode: int) -> None:
        """Clean up command response future."""
        self._pending_commands.pop(opcode, None)

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
            elif event_code in {MGMT_EV_CMD_COMPLETE, MGMT_EV_CMD_STATUS}:
                # Handle management command responses
                if param_len >= 3:
                    opcode = header[6] | (header[7] << 8)
                    status = header[8]
                    if opcode == MGMT_OP_LOAD_CONN_PARAM:
                        self._handle_load_conn_param_response(status, controller_idx)
                    elif (
                        opcode == MGMT_OP_GET_CONNECTIONS
                        and opcode in self._pending_commands
                    ):
                        # Handle GET_CONNECTIONS response for capability check
                        future = self._pending_commands.pop(opcode)
                        if not future.done():
                            # Return status and any response data
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
