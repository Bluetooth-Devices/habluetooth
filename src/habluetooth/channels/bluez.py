from __future__ import annotations

import asyncio
import logging
import socket
from asyncio import timeout as asyncio_timeout
from typing import Any, cast

from btsocket import btmgmt_protocol, btmgmt_socket

_LOGGER = logging.getLogger(__name__)


class BluetoothMGMTProtocol(asyncio.Protocol):
    """Bluetooth MGMT protocol."""

    def __init__(
        self, timeout: float, connection_mode_future: asyncio.Future[None]
    ) -> None:
        """Initialize the protocol."""
        self.future: asyncio.Future[btmgmt_protocol.Response] | None = None
        self.transport: asyncio.Transport | None = None
        self.timeout = timeout
        self.connection_mode_future = connection_mode_future
        self.loop = asyncio.get_running_loop()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        """Handle connection made."""
        if not self.connection_mode_future.done():
            self.connection_mode_future.set_result(None)
        self.transport = cast(asyncio.Transport, transport)

    def data_received(self, data: bytes) -> None:
        """Handle data received."""
        try:
            if (
                self.future
                and not self.future.done()
                and (response := btmgmt_protocol.reader(data))
                and response.cmd_response_frame
            ):
                self.future.set_result(response)
        except ValueError as ex:
            # ValueError: 47 is not a valid Events may happen on newer kernels
            # and we need to ignore these events
            _LOGGER.debug("Error parsing response: %s", ex)

    async def send(self, *args: Any) -> btmgmt_protocol.Response:
        """Send command."""
        pkt_objs = btmgmt_protocol.command(*args)
        self.future = self.loop.create_future()
        if self.transport is None:
            raise btmgmt_socket.BluetoothSocketError("Connection was closed")
        self.transport.write(b"".join(frame.octets for frame in pkt_objs if frame))
        cancel_timeout = self.loop.call_later(
            self.timeout, self._timeout_future, self.future
        )
        try:
            return await self.future
        finally:
            cancel_timeout.cancel()
            self.future = None

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

    def __init__(self, timeout: float) -> None:
        """Initialize the control class."""
        # Internal state
        self.timeout = timeout
        self.protocol: BluetoothMGMTProtocol | None = None
        self.sock: socket.socket | None = None

    async def close(self) -> None:
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
            async with asyncio_timeout(5):
                # _create_connection_transport accessed
                # directly to avoid SOCK_STREAM check
                # see https://bugs.python.org/issue38285
                _, protocol = await loop._create_connection_transport(  # type: ignore[attr-defined]
                    self.sock,
                    lambda: BluetoothMGMTProtocol(self.timeout, connection_made_future),
                    None,
                    None,
                )
                await connection_made_future
        except asyncio.TimeoutError:
            btmgmt_socket.close(self.sock)
            raise
        self.protocol = cast(BluetoothMGMTProtocol, protocol)
