from __future__ import annotations

import asyncio
import logging
import socket
from asyncio import timeout as asyncio_timeout
from typing import TYPE_CHECKING, cast

from btsocket import btmgmt_protocol, btmgmt_socket

from ..scanner import HaScanner

_LOGGER = logging.getLogger(__name__)
_int = int
_bytes = bytes
# Everything is little endian

HEADER_SIZE = 6
# Header is event_code (2 bytes), controller_idx (2 bytes), param_len (2 bytes)
DEVICE_FOUND = 0x0012
ADV_MONITOR_DEVICE_FOUND = 0x002F


class BluetoothMGMTProtocol:
    """Bluetooth MGMT protocol."""

    def __init__(
        self,
        connection_mode_future: asyncio.Future[None],
        scanners: dict[int, HaScanner],
    ) -> None:
        """Initialize the protocol."""
        self.transport: asyncio.Transport | None = None
        self.connection_mode_future = connection_mode_future
        self._buffer: bytes | None = None
        self._buffer_len = 0
        self._pos = 0
        self._scanners = scanners

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
            future.set_exception(asyncio.TimeoutError("Timeout waiting for response"))

    def connection_lost(self, exc: Exception | None) -> None:
        """Handle connection lost."""
        if exc:
            _LOGGER.warning("Bluetooth management socket connection lost: %s", exc)
        self.transport = None


class MGMTBluetoothCtl:
    """Class to control interfaces using the BlueZ management API."""

    def __init__(self, timeout: float, scanners: dict[int, HaScanner]) -> None:
        """Initialize the control class."""
        # Internal state
        self.timeout = timeout
        self.protocol: BluetoothMGMTProtocol | None = None
        self.sock: socket.socket | None = None
        self.scanners = scanners

    def close(self) -> None:
        """Close the management interface."""
        if self.protocol and self.protocol.transport:
            self.protocol.transport.close()
            self.protocol = None
        btmgmt_socket.close(self.sock)

    async def setup(self) -> None:
        """Set up management interface."""
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
                        connection_made_future, self.scanners
                    ),
                    None,
                    None,
                )
                await connection_made_future
        except asyncio.TimeoutError:
            btmgmt_socket.close(self.sock)
            raise
        self.protocol = cast(BluetoothMGMTProtocol, protocol)
