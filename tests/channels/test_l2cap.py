"""Tests for the L2CAP ATT socket transport."""

from __future__ import annotations

import asyncio
import errno
import os
import re
import socket
import sys
from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

import pytest
from bleak import BleakError

from habluetooth.channels.l2cap import (
    AF_BLUETOOTH,
    ATT_CID,
    BT_SECURITY_HIGH,
    BT_SECURITY_LOW,
    BT_SECURITY_MEDIUM,
    L2CAPSocket,
    _set_result_if_pending,
    _wait_connected,
    can_use_l2cap,
    make_sockaddr_l2,
    str_to_bdaddr,
)
from habluetooth.const import BDADDR_LE_RANDOM

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

pytestmark = [
    pytest.mark.asyncio,
    # L2CAP is Linux only; the transport drives the socket with add_reader /
    # add_writer / sock_sendall, which the Windows proactor event loop does not
    # implement. The selector loops on Linux and macOS run these fine.
    pytest.mark.skipif(
        sys.platform == "win32",
        reason="L2CAP transport needs a selector event loop (Linux/macOS)",
    ),
]

_SOURCE = "00:11:22:33:44:55"
_PEER = "AA:BB:CC:DD:EE:FF"


@pytest.fixture
def pair() -> Iterator[tuple[socket.socket, socket.socket]]:
    """Yield a connected socket pair and close both ends on teardown."""
    left, right = socket.socketpair()
    try:
        yield left, right
    finally:
        left.close()
        right.close()


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
    with pytest.raises(ValueError, match="Invalid MAC address"):
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
    pair: tuple[socket.socket, socket.socket],
    security_level: int,
    expected_calls: int,
) -> None:
    """A non-zero security level requests kernel LE security; zero skips it."""
    left, _right = pair
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
    sock.close()
    assert set_security.call_count == expected_calls


async def test_default_security_level_is_low(
    pair: tuple[socket.socket, socket.socket],
) -> None:
    """The connection starts at BT_SECURITY_LOW to match bluetoothd."""
    left, _right = pair
    sock = await _connect(left, on_data=lambda _d: None, on_close=lambda _e: None)
    assert sock.security_level == BT_SECURITY_LOW
    sock.close()


async def test_set_security_level_raises_and_tracks(
    pair: tuple[socket.socket, socket.socket],
) -> None:
    """Raising the level applies the socket option and records the new level."""
    left, _right = pair
    sock = await _connect(left, on_data=lambda _d: None, on_close=lambda _e: None)
    with patch("habluetooth.channels.l2cap._set_bt_security") as set_security:
        assert sock.set_security_level(BT_SECURITY_MEDIUM) is True
    assert sock.security_level == BT_SECURITY_MEDIUM
    set_security.assert_called_once_with(sock._sock, BT_SECURITY_MEDIUM)
    sock.close()


async def test_set_security_level_never_downgrades(
    pair: tuple[socket.socket, socket.socket],
) -> None:
    """A level at or below the current one is a no-op and is not applied."""
    left, _right = pair
    sock = await _connect(left, on_data=lambda _d: None, on_close=lambda _e: None)
    with patch("habluetooth.channels.l2cap._set_bt_security"):
        sock.set_security_level(BT_SECURITY_HIGH)
    with patch("habluetooth.channels.l2cap._set_bt_security") as set_security:
        assert sock.set_security_level(BT_SECURITY_MEDIUM) is False  # lower
        assert sock.set_security_level(BT_SECURITY_HIGH) is False  # equal
        set_security.assert_not_called()
    assert sock.security_level == BT_SECURITY_HIGH
    sock.close()


async def test_set_security_level_handles_oserror(
    pair: tuple[socket.socket, socket.socket],
) -> None:
    """A kernel refusal leaves the tracked level unchanged and returns False."""
    left, _right = pair
    sock = await _connect(left, on_data=lambda _d: None, on_close=lambda _e: None)
    with patch(
        "habluetooth.channels.l2cap._set_bt_security",
        side_effect=OSError(errno.EPERM, "denied"),
    ):
        assert sock.set_security_level(BT_SECURITY_MEDIUM) is False
    assert sock.security_level == BT_SECURITY_LOW
    sock.close()


async def test_create_connection_immediate_success(
    pair: tuple[socket.socket, socket.socket],
) -> None:
    """A connect that succeeds at once yields a live, readable socket."""
    left, right = pair
    received: list[bytes] = []
    closed: list[Exception | None] = []
    got = asyncio.Event()

    def on_data(data: bytes) -> None:
        received.append(data)
        got.set()

    sock = await _connect(left, on_data=on_data, on_close=closed.append)
    right.send(b"\x1b\x05\x00\xab")
    await got.wait()
    assert received == [b"\x1b\x05\x00\xab"]
    assert closed == []
    sock.close()


async def test_create_connection_in_progress_then_writable(
    pair: tuple[socket.socket, socket.socket],
) -> None:
    """An EINPROGRESS connect completes once the socket reports writable."""
    left, _right = pair
    sock = await _connect(
        left,
        on_data=lambda _d: None,
        on_close=lambda _e: None,
        connect_result=errno.EINPROGRESS,
    )
    assert sock._closed is False
    sock.close()


async def test_create_connection_so_error_fails_and_closes(
    pair: tuple[socket.socket, socket.socket],
) -> None:
    """A non-zero SO_ERROR after connect surfaces and closes the socket."""
    left, _right = pair
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


@pytest.mark.parametrize(
    ("connect_kwargs", "expected_errno"),
    [
        ({"connect_result": errno.EHOSTUNREACH}, errno.EHOSTUNREACH),
        ({"bind_result": errno.EPERM}, errno.EPERM),
    ],
)
async def test_create_connection_syscall_error_closes(
    pair: tuple[socket.socket, socket.socket],
    connect_kwargs: dict[str, int],
    expected_errno: int,
) -> None:
    """A hard bind/connect error is raised and the socket is closed."""
    left, _right = pair
    with pytest.raises(OSError, match=_strerror(expected_errno)) as exc_info:
        await _connect(
            left,
            on_data=lambda _d: None,
            on_close=lambda _e: None,
            **connect_kwargs,
        )
    assert exc_info.value.errno == expected_errno
    assert left.fileno() == -1


async def test_wait_connected_times_out(
    pair: tuple[socket.socket, socket.socket],
) -> None:
    """If the socket never reports writable, the connect times out."""
    loop = asyncio.get_running_loop()
    left, _right = pair
    with (
        patch.object(loop, "add_writer"),
        pytest.raises(TimeoutError),
    ):
        await _wait_connected(left, loop, 0.01)


# -- send / receive / teardown -------------------------------------------
async def test_send_writes_one_pdu(
    pair: tuple[socket.socket, socket.socket],
) -> None:
    """Send writes the PDU to the peer end of the channel."""
    left, right = pair
    sock = await _connect(left, on_data=lambda _d: None, on_close=lambda _e: None)
    await sock.send(b"\x02\x17\x00")
    assert right.recv(64) == b"\x02\x17\x00"
    sock.close()


async def test_send_serializes_concurrent_writes(
    pair: tuple[socket.socket, socket.socket],
) -> None:
    """Concurrent sends are serialized so one cannot stall another mid-write."""
    left, _right = pair
    sock = await _connect(left, on_data=lambda _d: None, on_close=lambda _e: None)
    order: list[tuple[str, bytes]] = []
    first_entered = asyncio.Event()
    release_first = asyncio.Event()

    async def fake_sendall(_sock: socket.socket, data: bytes) -> None:
        order.append(("enter", data))
        if data == b"A":
            first_entered.set()
            await release_first.wait()
        order.append(("exit", data))

    with patch.object(sock._loop, "sock_sendall", fake_sendall):
        first = asyncio.create_task(sock.send(b"A"))
        await first_entered.wait()
        second = asyncio.create_task(sock.send(b"B"))
        await asyncio.sleep(0)
        # The second send is blocked on the lock while the first is in flight.
        assert order == [("enter", b"A")]
        release_first.set()
        await asyncio.gather(first, second)
    assert order == [
        ("enter", b"A"),
        ("exit", b"A"),
        ("enter", b"B"),
        ("exit", b"B"),
    ]
    sock.close()


async def test_send_rechecks_closed_after_acquiring_lock(
    pair: tuple[socket.socket, socket.socket],
) -> None:
    """A send parked on the write lock raises BleakError if close() intervenes."""
    left, _right = pair
    sock = await _connect(left, on_data=lambda _d: None, on_close=lambda _e: None)
    in_flight = asyncio.Event()
    release = asyncio.Event()

    async def fake_sendall(_sock: socket.socket, _data: bytes) -> None:
        in_flight.set()
        await release.wait()

    with patch.object(sock._loop, "sock_sendall", fake_sendall):
        first = asyncio.create_task(sock.send(b"A"))
        await in_flight.wait()
        second = asyncio.create_task(sock.send(b"B"))
        await asyncio.sleep(0)  # the second send parks on the held lock
        sock.close()  # tear down while the second send is waiting
        release.set()
        await first  # the first send already passed the closed check
        with pytest.raises(BleakError, match="closed"):
            await second


async def test_send_error_tears_down_channel(
    pair: tuple[socket.socket, socket.socket],
) -> None:
    """A write-side socket error closes the channel and notifies on_close once."""
    left, _right = pair
    closed: list[Exception | None] = []
    sock = await _connect(left, on_data=lambda _d: None, on_close=closed.append)
    boom = OSError("epipe")

    async def fake_sendall(_sock: socket.socket, _data: bytes) -> None:
        raise boom

    with (
        patch.object(sock._loop, "sock_sendall", fake_sendall),
        pytest.raises(OSError, match="epipe"),
    ):
        await sock.send(b"A")
    assert sock._closed is True
    assert closed == [boom]


async def test_send_after_close_raises(
    pair: tuple[socket.socket, socket.socket],
) -> None:
    """Writing to a closed channel raises rather than touching the socket."""
    left, _right = pair
    sock = await _connect(left, on_data=lambda _d: None, on_close=lambda _e: None)
    sock.close()
    with pytest.raises(BleakError, match="closed"):
        await sock.send(b"\x02\x17\x00")


async def test_peer_disconnect_reports_close_once(
    pair: tuple[socket.socket, socket.socket],
) -> None:
    """A peer hangup is reported to on_close exactly once with no error."""
    left, right = pair
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


async def test_read_ready_ignores_would_block(
    pair: tuple[socket.socket, socket.socket],
) -> None:
    """A spurious wakeup with no data is a no-op, not a disconnect."""
    left, _right = pair
    received: list[bytes] = []
    closed: list[Exception | None] = []
    sock = await _connect(left, on_data=received.append, on_close=closed.append)
    sock._sock = Mock(recv=Mock(side_effect=BlockingIOError))
    sock._read_ready()
    assert received == []
    assert closed == []
    assert sock._closed is False


async def test_read_ready_reports_socket_error(
    pair: tuple[socket.socket, socket.socket],
) -> None:
    """A read error tears the channel down and reports the exception once."""
    left, _right = pair
    closed: list[Exception | None] = []
    sock = await _connect(left, on_data=lambda _d: None, on_close=closed.append)
    fd = sock._sock.fileno()
    boom = OSError("boom")
    sock._sock = Mock(recv=Mock(side_effect=boom), fileno=Mock(return_value=fd))
    sock._read_ready()
    assert closed == [boom]
    assert sock._closed is True


async def test_close_is_idempotent(
    pair: tuple[socket.socket, socket.socket],
) -> None:
    """Closing twice is safe and does not double-report."""
    left, _right = pair
    closed: list[Exception | None] = []
    sock = await _connect(left, on_data=lambda _d: None, on_close=closed.append)
    sock.close()
    sock.close()
    # A subsequent failure does not call on_close again.
    sock._fail(None)
    assert closed == []


async def test_set_result_if_pending_is_idempotent() -> None:
    """Resolving an already-finished future is a no-op."""
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[None] = loop.create_future()
    _set_result_if_pending(fut)
    _set_result_if_pending(fut)
    assert fut.result() is None


async def test_can_use_l2cap_true_when_socket_opens() -> None:
    """can_use_l2cap returns True when an L2CAP socket can be created."""
    sock = Mock()
    with patch("socket.socket", return_value=sock) as make_socket:
        assert can_use_l2cap() is True
    make_socket.assert_called_once()
    sock.close.assert_called_once()


async def test_can_use_l2cap_false_without_permission() -> None:
    """can_use_l2cap returns False when the socket cannot be opened."""
    with patch("socket.socket", side_effect=PermissionError):
        assert can_use_l2cap() is False
