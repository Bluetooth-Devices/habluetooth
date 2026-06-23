"""Tests for the L2CAP ATT socket transport."""

from __future__ import annotations

import asyncio
import errno
import os
import re
import socket
from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

import pytest
from bleak import BleakError

from habluetooth.channels.l2cap import (
    AF_BLUETOOTH,
    ATT_CID,
    L2CAPSocket,
    _set_result_if_pending,
    make_sockaddr_l2,
    str_to_bdaddr,
)
from habluetooth.const import BDADDR_LE_RANDOM

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = pytest.mark.asyncio

_SOURCE = "00:11:22:33:44:55"
_PEER = "AA:BB:CC:DD:EE:FF"


async def _connect(
    left: socket.socket,
    *,
    on_data: Callable[[bytes], None],
    on_close: Callable[[Exception | None], None],
    connect_result: int = 0,
    bind_result: int = 0,
    timeout: float = 1.0,
) -> L2CAPSocket:
    """Drive create_connection over an injected socket with patched syscalls."""
    with (
        patch("habluetooth.channels.l2cap._set_bt_security"),
        patch("habluetooth.channels.l2cap._bind_fd", return_value=bind_result),
        patch("habluetooth.channels.l2cap._connect_fd", return_value=connect_result),
    ):
        return await L2CAPSocket.create_connection(
            source=_SOURCE,
            address=_PEER,
            address_type=BDADDR_LE_RANDOM,
            on_data=on_data,
            on_close=on_close,
            timeout=timeout,
            sock=left,
        )


def _strerror(err: int) -> str:
    """Return the platform error string for ``err`` as a regex-safe pattern."""
    return re.escape(os.strerror(err))


# -- address packing ------------------------------------------------------
async def test_str_to_bdaddr_reverses_octets() -> None:
    """A textual BD_ADDR is packed least-significant-octet first."""
    assert str_to_bdaddr("AA:BB:CC:DD:EE:FF") == bytes(
        [0xFF, 0xEE, 0xDD, 0xCC, 0xBB, 0xAA]
    )


@pytest.mark.parametrize("bad", ["AA:BB:CC", "", "AA:BB:CC:DD:EE:FF:00"])
async def test_str_to_bdaddr_rejects_malformed(bad: str) -> None:
    """An address without exactly six octets is rejected."""
    with pytest.raises(ValueError, match="invalid Bluetooth address"):
        str_to_bdaddr(bad)


async def test_make_sockaddr_l2_layout() -> None:
    """The packed sockaddr carries the family, channel, address, and type."""
    addr = make_sockaddr_l2(_PEER, ATT_CID, BDADDR_LE_RANDOM)
    assert addr.l2_family == AF_BLUETOOTH
    assert addr.l2_psm == 0
    assert bytes(addr.l2_bdaddr) == bytes([0xFF, 0xEE, 0xDD, 0xCC, 0xBB, 0xAA])
    assert addr.l2_cid == ATT_CID
    assert addr.l2_bdaddr_type == BDADDR_LE_RANDOM


# -- connect handshake ----------------------------------------------------
@pytest.mark.parametrize(
    ("security_level", "expected_calls"),
    [(2, 1), (0, 0)],
)
async def test_create_connection_security_level(
    security_level: int, expected_calls: int
) -> None:
    """A non-zero security level requests kernel LE security; zero skips it."""
    left, right = socket.socketpair()
    with (
        patch("habluetooth.channels.l2cap._set_bt_security") as set_security,
        patch("habluetooth.channels.l2cap._bind_fd", return_value=0),
        patch("habluetooth.channels.l2cap._connect_fd", return_value=0),
    ):
        sock = await L2CAPSocket.create_connection(
            source=_SOURCE,
            address=_PEER,
            address_type=BDADDR_LE_RANDOM,
            on_data=lambda _d: None,
            on_close=lambda _e: None,
            timeout=1.0,
            security_level=security_level,
            sock=left,
        )
    try:
        assert set_security.call_count == expected_calls
    finally:
        sock.close()
        right.close()


async def test_create_connection_immediate_success() -> None:
    """A connect that succeeds at once yields a live, readable socket."""
    left, right = socket.socketpair()
    received: list[bytes] = []
    closed: list[Exception | None] = []
    got = asyncio.Event()

    def on_data(data: bytes) -> None:
        received.append(data)
        got.set()

    sock = await _connect(left, on_data=on_data, on_close=closed.append)
    try:
        right.send(b"\x1b\x05\x00\xab")
        await got.wait()
        assert received == [b"\x1b\x05\x00\xab"]
        assert closed == []
    finally:
        sock.close()
        right.close()


async def test_create_connection_in_progress_then_writable() -> None:
    """An EINPROGRESS connect completes once the socket reports writable."""
    left, right = socket.socketpair()
    sock = await _connect(
        left,
        on_data=lambda _d: None,
        on_close=lambda _e: None,
        connect_result=errno.EINPROGRESS,
    )
    try:
        assert sock._closed is False
    finally:
        sock.close()
        right.close()


async def test_create_connection_so_error_fails_and_closes() -> None:
    """A non-zero SO_ERROR after connect surfaces and closes the socket."""
    left, right = socket.socketpair()
    with (
        patch(
            "habluetooth.channels.l2cap._so_error",
            return_value=errno.EHOSTUNREACH,
        ),
        pytest.raises(OSError, match=_strerror(errno.EHOSTUNREACH)) as exc_info,
    ):
        await _connect(
            left,
            on_data=lambda _d: None,
            on_close=lambda _e: None,
            connect_result=errno.EINPROGRESS,
        )
    assert exc_info.value.errno == errno.EHOSTUNREACH
    assert left.fileno() == -1  # the injected socket was closed on failure
    right.close()


async def test_create_connection_immediate_error_closes() -> None:
    """A hard connect error is raised and the socket is closed."""
    left, right = socket.socketpair()
    with pytest.raises(OSError, match=_strerror(errno.EHOSTUNREACH)) as exc_info:
        await _connect(
            left,
            on_data=lambda _d: None,
            on_close=lambda _e: None,
            connect_result=errno.EHOSTUNREACH,
        )
    assert exc_info.value.errno == errno.EHOSTUNREACH
    assert left.fileno() == -1
    right.close()


async def test_create_connection_bind_error_closes() -> None:
    """A bind failure is raised and the socket is closed."""
    left, right = socket.socketpair()
    with pytest.raises(OSError, match=_strerror(errno.EPERM)) as exc_info:
        await _connect(
            left,
            on_data=lambda _d: None,
            on_close=lambda _e: None,
            bind_result=errno.EPERM,
        )
    assert exc_info.value.errno == errno.EPERM
    assert left.fileno() == -1
    right.close()


async def test_wait_connected_times_out() -> None:
    """If the socket never reports writable, the connect times out."""
    loop = asyncio.get_running_loop()
    left, right = socket.socketpair()
    try:
        with (
            patch.object(loop, "add_writer"),
            pytest.raises(TimeoutError),
        ):
            await L2CAPSocket._wait_connected(left, loop, 0.01)
    finally:
        left.close()
        right.close()


# -- send / receive / teardown -------------------------------------------
async def test_send_writes_one_pdu() -> None:
    """Send writes the PDU to the peer end of the channel."""
    left, right = socket.socketpair()
    sock = await _connect(left, on_data=lambda _d: None, on_close=lambda _e: None)
    try:
        await sock.send(b"\x02\x17\x00")
        assert right.recv(64) == b"\x02\x17\x00"
    finally:
        sock.close()
        right.close()


async def test_send_after_close_raises() -> None:
    """Writing to a closed channel raises rather than touching the socket."""
    left, right = socket.socketpair()
    sock = await _connect(left, on_data=lambda _d: None, on_close=lambda _e: None)
    sock.close()
    with pytest.raises(BleakError, match="closed"):
        await sock.send(b"\x02\x17\x00")
    right.close()


async def test_peer_disconnect_reports_close_once() -> None:
    """A peer hangup is reported to on_close exactly once with no error."""
    left, right = socket.socketpair()
    closed: list[Exception | None] = []
    hung_up = asyncio.Event()

    def on_close(exc: Exception | None) -> None:
        closed.append(exc)
        hung_up.set()

    sock = await _connect(left, on_data=lambda _d: None, on_close=on_close)
    right.close()
    await hung_up.wait()
    assert closed == [None]
    assert sock._closed is True


async def test_read_ready_ignores_would_block() -> None:
    """A spurious wakeup with no data is a no-op, not a disconnect."""
    left, right = socket.socketpair()
    received: list[bytes] = []
    closed: list[Exception | None] = []
    sock = await _connect(left, on_data=received.append, on_close=closed.append)
    try:
        sock._sock = Mock(recv=Mock(side_effect=BlockingIOError))
        sock._read_ready()
        assert received == []
        assert closed == []
        assert sock._closed is False
    finally:
        left.close()
        right.close()


async def test_read_ready_reports_socket_error() -> None:
    """A read error tears the channel down and reports the exception once."""
    left, right = socket.socketpair()
    closed: list[Exception | None] = []
    sock = await _connect(left, on_data=lambda _d: None, on_close=closed.append)
    fd = sock._sock.fileno()
    boom = OSError("boom")
    sock._sock = Mock(recv=Mock(side_effect=boom), fileno=Mock(return_value=fd))
    sock._read_ready()
    assert closed == [boom]
    assert sock._closed is True
    left.close()
    right.close()


async def test_close_is_idempotent() -> None:
    """Closing twice is safe and does not double-report."""
    left, right = socket.socketpair()
    closed: list[Exception | None] = []
    sock = await _connect(left, on_data=lambda _d: None, on_close=closed.append)
    sock.close()
    sock.close()
    # A subsequent failure does not call on_close again.
    sock._fail(None)
    assert closed == []
    right.close()


async def test_set_result_if_pending_is_idempotent() -> None:
    """Resolving an already-finished future is a no-op."""
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[None] = loop.create_future()
    _set_result_if_pending(fut)
    _set_result_if_pending(fut)
    assert fut.result() is None
