"""
L2CAP ATT socket transport for the kernel/L2CAP backend.

This opens a raw L2CAP connection-oriented socket to a peripheral on the ATT
fixed channel (CID ``0x0004``) and exposes it as an asyncio push transport: one
SEQPACKET datagram in, one ATT PDU out. It is the thing that feeds
:class:`habluetooth.channels.att.ATTClient` once a peer is connected.

CPython's :mod:`socket` cannot express the Bluetooth ``sockaddr_l2`` (it has no
field for ``l2_cid`` or ``l2_bdaddr_type``), so the address is packed by hand
and ``bind``/``connect`` are issued through libc via :mod:`ctypes` on the raw
file descriptor; everything after the connect handshake is ordinary asyncio
(:meth:`~asyncio.loop.add_reader` for the inbound stream,
:meth:`~asyncio.loop.sock_sendall` for backpressured writes).

``SOCK_SEQPACKET`` preserves datagram boundaries, so each ``recv`` yields
exactly one ATT PDU and the codec never has to reframe the stream. The libc
``bind``/``connect`` shims are the only Linux/Bluetooth specific lines; the
orchestration around them is exercised over an ordinary socket pair, so the
module is unit-testable without Bluetooth hardware.
"""

from __future__ import annotations

import asyncio
import ctypes
import errno
import functools
import logging
import os
import socket
import struct
from typing import TYPE_CHECKING

from bleak import BleakError
from bluetooth_data_tools import mac_to_int

from ..const import BDADDR_LE_PUBLIC

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import NoReturn

_LOGGER = logging.getLogger(__name__)

# -- Bluetooth socket constants -------------------------------------------
# CPython exposes these on Linux only (and not at all on python-build-standalone
# builds without the bluetooth headers), so they are pinned here as literals
# rather than read from the socket module.
AF_BLUETOOTH = 31
BTPROTO_L2CAP = 0
ATT_CID = 0x0004

# setsockopt(SOL_BLUETOOTH, BT_SECURITY, struct bt_security{level, key_size})
SOL_BLUETOOTH = 274
BT_SECURITY = 4
BT_SECURITY_LOW = 1
BT_SECURITY_MEDIUM = 2
BT_SECURITY_HIGH = 3

# One SEQPACKET recv yields one ATT PDU; the largest legal ATT MTU is 517, so a
# 1024 byte buffer cannot truncate a PDU.
_RECV_BUFSIZE = 1024


class _SockaddrL2(ctypes.Structure):
    """The Linux ``struct sockaddr_l2`` (see ``include/net/bluetooth/l2cap.h``)."""

    _fields_ = (
        ("l2_family", ctypes.c_ushort),
        ("l2_psm", ctypes.c_ushort),
        ("l2_bdaddr", ctypes.c_ubyte * 6),
        ("l2_cid", ctypes.c_ushort),
        ("l2_bdaddr_type", ctypes.c_ubyte),
    )


def str_to_bdaddr(address: str) -> bytes:
    """
    Pack ``AA:BB:CC:DD:EE:FF`` into the 6 little-endian bytes the kernel wants.

    BD_ADDRs go on the wire least-significant-octet first, so the textual
    address is reversed. Raises ``ValueError`` for a malformed address.
    """
    return mac_to_int(address).to_bytes(6, "little")


def make_sockaddr_l2(address: str, cid: int, bdaddr_type: int) -> _SockaddrL2:
    """Build a ``sockaddr_l2`` for ``address`` on the given fixed channel."""
    addr = _SockaddrL2()
    addr.l2_family = AF_BLUETOOTH
    addr.l2_psm = 0
    addr.l2_bdaddr = (ctypes.c_ubyte * 6)(*str_to_bdaddr(address))
    addr.l2_cid = cid
    addr.l2_bdaddr_type = bdaddr_type
    return addr


@functools.cache
def _get_libc() -> ctypes.CDLL:  # pragma: no cover - libc load is platform glue
    """Return a cached errno-aware libc handle for the raw bind/connect calls."""
    return ctypes.CDLL(None, use_errno=True)


def _bind_fd(fd: int, addr: _SockaddrL2) -> int:  # pragma: no cover - syscall shim
    """``bind(2)`` the raw fd; return 0 on success or the errno on failure."""
    if _get_libc().bind(fd, ctypes.byref(addr), ctypes.sizeof(addr)) != 0:
        return ctypes.get_errno()
    return 0


def _connect_fd(fd: int, addr: _SockaddrL2) -> int:  # pragma: no cover - syscall
    """``connect(2)`` the raw fd; return 0, EINPROGRESS, or the errno."""
    if _get_libc().connect(fd, ctypes.byref(addr), ctypes.sizeof(addr)) != 0:
        return ctypes.get_errno()
    return 0


class L2CAPSocket:
    """
    An open L2CAP ATT channel to a single peer, driven by asyncio.

    Construct one with :meth:`create_connection`; it registers a reader so that
    every inbound PDU is handed to ``on_data`` and a transport failure (peer
    disconnect, socket error) is reported once to ``on_close``. Outbound PDUs go
    through :meth:`send`, which is backpressure aware. ``on_data`` runs in the
    event loop read callback, so it must not block.

    The loop's reader holds a reference to this socket (and thus to the
    callbacks), so the owner must call :meth:`close` to release it; a peer
    disconnect or read error closes it automatically.
    """

    def __init__(
        self,
        sock: socket.socket,
        loop: asyncio.AbstractEventLoop,
        on_data: Callable[[bytes], None],
        on_close: Callable[[Exception | None], None],
    ) -> None:
        """Wrap an already-connected socket and start reading from it."""
        self._sock = sock
        self._loop = loop
        self._on_data = on_data
        self._on_close = on_close
        self._closed = False
        self._write_lock = asyncio.Lock()
        loop.add_reader(sock.fileno(), self._read_ready)

    @classmethod
    async def create_connection(
        cls,
        *,
        source: str,
        address: str,
        address_type: int,
        on_data: Callable[[bytes], None],
        on_close: Callable[[Exception | None], None],
        timeout: float,
        security_level: int = BT_SECURITY_MEDIUM,
        sock: socket.socket | None = None,
    ) -> L2CAPSocket:
        """
        Open an L2CAP ATT channel from adapter ``source`` to ``address``.

        ``source`` is the local adapter's public BD_ADDR (it selects which
        adapter the connection goes out of); ``address``/``address_type`` are the
        peer. ``security_level`` requests kernel-driven LE encryption when bonded
        keys exist; pass 0 to leave it unset. ``sock`` is injectable for testing.
        """
        loop = asyncio.get_running_loop()
        if sock is None:  # pragma: no cover - real socket needs a Linux adapter
            sock = socket.socket(AF_BLUETOOTH, socket.SOCK_SEQPACKET, BTPROTO_L2CAP)
        try:
            sock.setblocking(False)
            if security_level:
                _set_bt_security(sock, security_level)
            _bind(sock, source)
            await _connect(sock, address, address_type, loop, timeout)
        except BaseException:
            sock.close()
            raise
        return cls(sock, loop, on_data, on_close)

    async def send(self, data: bytes) -> None:
        """
        Write one ATT PDU, awaiting socket writability under backpressure.

        Serialized with a lock: ATTClient may call this concurrently (e.g. an
        indication confirmation or a write-without-response alongside a pending
        request), and ``loop.sock_sendall`` is not safe to run concurrently on
        one fd (a second backpressured call would replace the first's writer and
        stall it). SEQPACKET keeps each write framed as one PDU.
        """
        if self._closed:
            msg = "L2CAP socket is closed"
            raise BleakError(msg)
        async with self._write_lock:
            await self._loop.sock_sendall(self._sock, data)

    def _read_ready(self) -> None:
        """Read one inbound PDU and hand it to ``on_data`` (reader callback)."""
        try:
            data = self._sock.recv(_RECV_BUFSIZE)
        except (BlockingIOError, InterruptedError):
            return
        except OSError as exc:
            self._fail(exc)
            return
        if not data:
            # A zero-length SEQPACKET read means the peer hung up.
            self._fail(None)
            return
        self._on_data(data)

    def _fail(self, exc: Exception | None) -> None:
        """Tear down once and report the loss to ``on_close`` exactly once."""
        if self._closed:
            return
        self.close()
        self._on_close(exc)

    def close(self) -> None:
        """Stop reading and close the socket; idempotent."""
        if self._closed:
            return
        self._closed = True
        self._loop.remove_reader(self._sock.fileno())
        self._sock.close()


def _bind(sock: socket.socket, source: str) -> None:
    """Bind the socket to the local adapter so the route is deterministic."""
    addr = make_sockaddr_l2(source, ATT_CID, BDADDR_LE_PUBLIC)
    if (err := _bind_fd(sock.fileno(), addr)) != 0:
        _raise_errno(err)


async def _connect(
    sock: socket.socket,
    address: str,
    address_type: int,
    loop: asyncio.AbstractEventLoop,
    timeout: float,
) -> None:
    """Issue the non-blocking connect and await the result via SO_ERROR."""
    addr = make_sockaddr_l2(address, ATT_CID, address_type)
    err = _connect_fd(sock.fileno(), addr)
    if err in (errno.EINPROGRESS, errno.EALREADY, errno.EAGAIN):
        await _wait_connected(sock, loop, timeout)
    elif err != 0:
        _raise_errno(err)


async def _wait_connected(
    sock: socket.socket,
    loop: asyncio.AbstractEventLoop,
    timeout: float,
) -> None:
    """Wait for the in-progress connect to settle, then check SO_ERROR."""
    fut: asyncio.Future[None] = loop.create_future()
    fd = sock.fileno()
    loop.add_writer(fd, _set_result_if_pending, fut)
    try:
        async with asyncio.timeout(timeout):
            await fut
    finally:
        loop.remove_writer(fd)
    if (err := _so_error(sock)) != 0:
        _raise_errno(err)


def _raise_errno(err: int) -> NoReturn:
    """Raise an ``OSError`` carrying ``err`` and its platform message."""
    raise OSError(err, os.strerror(err))


def _set_bt_security(  # pragma: no cover - BT-only setsockopt
    sock: socket.socket, level: int
) -> None:
    """Request a kernel-driven LE security level on the socket."""
    sock.setsockopt(SOL_BLUETOOTH, BT_SECURITY, struct.pack("BB", level, 0))


def _so_error(sock: socket.socket) -> int:
    """Return the socket's pending error (``SO_ERROR``), 0 if none."""
    return sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)


def _set_result_if_pending(fut: asyncio.Future[None]) -> None:
    """Resolve ``fut`` if it has not already completed (writer callback)."""
    if not fut.done():
        fut.set_result(None)
