"""Tests for the kernel/L2CAP GATT client backend."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from bleak import BleakError
from bleak.backends.device import BLEDevice

from habluetooth.client_mgmt import HaMgmtClient, MgmtClientData
from habluetooth.const import BDADDR_LE_RANDOM

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from bleak.backends.characteristic import BleakGATTCharacteristic

_CHAR_UUID = "00002a37-0000-1000-8000-00805f9b34fb"
_SERVICE_UUID = "0000180d-0000-1000-8000-00805f9b34fb"
_CCCD_DESC_UUID = "00002902-0000-1000-8000-00805f9b34fb"

pytestmark = pytest.mark.asyncio

_ADAPTER = "00:11:22:33:44:55"
_PEER = "AA:BB:CC:DD:EE:FF"

# Opcodes used to build server responses.
_ERROR_RSP = 0x01
_MTU_RSP = 0x03
_FIND_INFO_RSP = 0x05
_READ_BY_TYPE_RSP = 0x09
_READ_RSP = 0x0B
_READ_BY_GROUP_TYPE_RSP = 0x11
_WRITE_RSP = 0x13
_NTF = 0x1B
_ERR_ATTRIBUTE_NOT_FOUND = 0x0A

# Discovered layout: heart-rate service 0x180D with one characteristic 0x2A37
# (value handle 0x0003) and, when present, a CCCD at 0x0004.
_CHAR_READ_NOTIFY = 0x12
_VALUE = b"\x01\x02\x03"


def _error(req_opcode: int, handle: int, code: int) -> bytes:
    return (
        bytes([_ERROR_RSP, req_opcode]) + handle.to_bytes(2, "little") + bytes([code])
    )


def make_responder(
    char_props: int = _CHAR_READ_NOTIFY,
    *,
    has_cccd: bool = True,
    fail_write: bool = False,
) -> Callable[[bytes], bytes | None]:
    """Build a minimal ATT server for one service/characteristic."""
    svc_end = 0x0004 if has_cccd else 0x0003

    def services(req: bytes) -> bytes:  # READ_BY_GROUP_TYPE_REQ
        if int.from_bytes(req[1:3], "little") > 0x0001:
            return _error(0x10, 0x0001, _ERR_ATTRIBUTE_NOT_FOUND)
        entry = (
            (0x0001).to_bytes(2, "little")
            + svc_end.to_bytes(2, "little")
            + (0x180D).to_bytes(2, "little")
        )
        return bytes([_READ_BY_GROUP_TYPE_RSP, len(entry)]) + entry

    def characteristics(req: bytes) -> bytes:  # READ_BY_TYPE_REQ
        if int.from_bytes(req[1:3], "little") > 0x0002:
            return _error(0x08, 0x0002, _ERR_ATTRIBUTE_NOT_FOUND)
        value = (
            bytes([char_props])
            + (0x0003).to_bytes(2, "little")
            + (0x2A37).to_bytes(2, "little")
        )
        entry = (0x0002).to_bytes(2, "little") + value
        return bytes([_READ_BY_TYPE_RSP, len(entry)]) + entry

    def descriptors(req: bytes) -> bytes:  # FIND_INFORMATION_REQ
        if int.from_bytes(req[1:3], "little") > 0x0004:
            return _error(0x04, 0x0004, _ERR_ATTRIBUTE_NOT_FOUND)
        entry = (0x0004).to_bytes(2, "little") + (0x2902).to_bytes(2, "little")
        return bytes([_FIND_INFO_RSP, 0x01]) + entry

    write_rsp = (
        _error(0x12, 0x0004, 0x03) if fail_write else bytes([_WRITE_RSP])
    )  # WRITE_REQ: an error (write not permitted) or success
    handlers: dict[int, Callable[[bytes], bytes | None]] = {
        0x02: lambda _req: bytes([_MTU_RSP]) + (247).to_bytes(2, "little"),
        0x10: services,
        0x08: characteristics,
        0x04: descriptors,
        0x0A: lambda _req: bytes([_READ_RSP]) + _VALUE,  # READ_REQ
        0x12: lambda _req: write_rsp,
        0x52: lambda _req: None,  # WRITE_CMD (no response)
    }

    def responder(req: bytes) -> bytes | None:
        if (handler := handlers.get(req[0])) is None:
            msg = f"unexpected request opcode 0x{req[0]:02x}"
            raise AssertionError(msg)
        return handler(req)

    return responder


class FakeTransport:
    """Stand-in for L2CAPSocket that answers ATT PDUs from a responder."""

    def __init__(
        self,
        responder: Callable[[bytes], bytes | None],
        on_data: Callable[[bytes], None],
        on_close: Callable[[Exception | None], None],
    ) -> None:
        self._responder = responder
        self._on_data = on_data
        self._on_close = on_close
        self.sent: list[bytes] = []
        self.closed = False

    async def send(self, data: bytes) -> None:
        self.sent.append(bytes(data))
        if (resp := self._responder(bytes(data))) is not None:
            self._on_data(resp)

    def close(self) -> None:
        self.closed = True

    def inject(self, data: bytes) -> None:
        """Simulate a server-initiated PDU (e.g. a notification)."""
        self._on_data(data)

    def drop(self, exc: Exception | None = None) -> None:
        """Simulate an unexpected transport failure."""
        self._on_close(exc)


class FakeScanner:
    """Minimal scanner exposing the connecting() pause context manager."""

    def __init__(self) -> None:
        self.connecting_calls = 0

    @contextmanager
    def connecting(self) -> Generator[None, None, None]:
        self.connecting_calls += 1
        yield


def _ble_device() -> BLEDevice:
    return BLEDevice(
        _PEER,
        "test-device",
        {"source": _ADAPTER, "address_type": BDADDR_LE_RANDOM},
    )


@contextmanager
def _patch_transport(
    responder: Callable[[bytes], bytes | None],
    holder: dict[str, FakeTransport],
) -> Generator[None, None, None]:
    async def fake_create_connection(
        *,
        on_data: Callable[[bytes], None],
        on_close: Callable[[Exception | None], None],
        **_kwargs: object,
    ) -> FakeTransport:
        transport = FakeTransport(responder, on_data, on_close)
        holder["transport"] = transport
        return transport

    with patch(
        "habluetooth.client_mgmt.L2CAPSocket.create_connection",
        fake_create_connection,
    ):
        yield


def _make_client(
    disconnected_callback: Callable[[], None] | None = None,
) -> tuple[HaMgmtClient, FakeScanner]:
    scanner = FakeScanner()
    client = HaMgmtClient(
        _ble_device(),
        client_data=MgmtClientData(adapter_address=_ADAPTER, scanner=scanner),
        timeout=5.0,
        disconnected_callback=disconnected_callback,
    )
    return client, scanner


async def _connect(
    holder: dict[str, FakeTransport],
    responder: Callable[[bytes], bytes | None] | None = None,
    disconnected_callback: Callable[[], None] | None = None,
) -> tuple[HaMgmtClient, FakeScanner]:
    client, scanner = _make_client(disconnected_callback)
    with _patch_transport(responder or make_responder(), holder):
        await client.connect(False)
    return client, scanner


def _char(client: HaMgmtClient) -> BleakGATTCharacteristic:
    """Return the discovered 0x2A37 characteristic (asserts it was discovered)."""
    assert client.services is not None
    char = client.services.get_characteristic(_CHAR_UUID)
    assert char is not None
    return char


# -- connect / discover ---------------------------------------------------
async def test_connect_discovers_services() -> None:
    """Connect opens the channel, exchanges MTU, and builds the GATT tree."""
    holder: dict[str, FakeTransport] = {}
    client, scanner = await _connect(holder)
    assert client.is_connected is True
    assert client.mtu_size == 247
    assert scanner.connecting_calls == 1
    services = client.services
    assert services is not None
    assert services.get_service(_SERVICE_UUID) is not None
    char = _char(client)
    assert char.handle == 0x0003  # value handle drives operations
    assert "notify" in char.properties
    assert char.get_descriptor(_CCCD_DESC_UUID) is not None
    assert char.max_write_without_response_size == 244  # MTU 247 - 3


async def test_connect_rejects_when_already_connected() -> None:
    """A second connect on a live client raises rather than re-linking."""
    holder: dict[str, FakeTransport] = {}
    client, _scanner = await _connect(holder)
    with pytest.raises(BleakError, match="already connected"):
        await client.connect(False)


async def test_connect_ignores_pair_flag() -> None:
    """connect(pair=True) still connects; mgmt pairing is deferred."""
    holder: dict[str, FakeTransport] = {}
    client, _scanner = _make_client()
    with _patch_transport(make_responder(), holder):
        await client.connect(True)
    assert client.is_connected is True


async def test_connect_failure_tears_down() -> None:
    """A transport failure during connect leaves the client disconnected."""
    client, _scanner = _make_client()

    async def boom(**_kwargs: object) -> FakeTransport:
        raise OSError(113, "No route to host")

    with (
        patch("habluetooth.client_mgmt.L2CAPSocket.create_connection", boom),
        pytest.raises(OSError, match="No route to host"),
    ):
        await client.connect(False)
    assert client.is_connected is False
    assert client.mtu_size == 23


# -- GATT operations ------------------------------------------------------
async def test_read_gatt_char() -> None:
    """Reading a characteristic returns the value as a bytearray."""
    holder: dict[str, FakeTransport] = {}
    client, _scanner = await _connect(holder)
    assert await client.read_gatt_char(_char(client)) == bytearray(_VALUE)


async def test_read_gatt_descriptor() -> None:
    """Reading a descriptor returns the value as a bytearray."""
    holder: dict[str, FakeTransport] = {}
    client, _scanner = await _connect(holder)
    cccd = _char(client).get_descriptor(_CCCD_DESC_UUID)
    assert cccd is not None
    assert await client.read_gatt_descriptor(cccd) == bytearray(_VALUE)


async def test_write_gatt_char_with_response() -> None:
    """A write with response uses a Write Request."""
    holder: dict[str, FakeTransport] = {}
    client, _scanner = await _connect(holder)
    await client.write_gatt_char(_char(client), b"\xaa", response=True)
    assert holder["transport"].sent[-1] == b"\x12\x03\x00\xaa"  # WRITE_REQ


async def test_write_gatt_char_without_response() -> None:
    """A write without response uses a Write Command (no reply expected)."""
    holder: dict[str, FakeTransport] = {}
    client, _scanner = await _connect(holder)
    await client.write_gatt_char(_char(client), b"\xbb", response=False)
    assert holder["transport"].sent[-1] == b"\x52\x03\x00\xbb"  # WRITE_CMD


async def test_write_gatt_descriptor() -> None:
    """Writing a descriptor uses a Write Request against its handle."""
    holder: dict[str, FakeTransport] = {}
    client, _scanner = await _connect(holder)
    cccd = _char(client).get_descriptor(_CCCD_DESC_UUID)
    assert cccd is not None
    await client.write_gatt_descriptor(cccd, b"\x01\x00")
    assert holder["transport"].sent[-1] == b"\x12\x04\x00\x01\x00"


# -- notifications --------------------------------------------------------
async def test_start_notify_writes_cccd_and_routes() -> None:
    """start_notify writes the notify CCCD and delivers inbound notifications."""
    holder: dict[str, FakeTransport] = {}
    client, _scanner = await _connect(holder)
    received: list[bytearray] = []
    await client.start_notify(_char(client), received.append)
    assert holder["transport"].sent[-1] == b"\x12\x04\x00\x01\x00"  # CCCD notify
    holder["transport"].inject(bytes([_NTF]) + (0x0003).to_bytes(2, "little") + b"\x09")
    assert received == [bytearray(b"\x09")]


async def test_start_notify_uses_indicate_when_only_indicate() -> None:
    """An indicate-only characteristic gets the indicate CCCD value."""
    holder: dict[str, FakeTransport] = {}
    client, _scanner = await _connect(holder, make_responder(0x20))  # indicate only
    await client.start_notify(_char(client), lambda _d: None)
    assert holder["transport"].sent[-1] == b"\x12\x04\x00\x02\x00"  # CCCD indicate


async def test_start_notify_rejects_unsupported_characteristic() -> None:
    """A characteristic without notify/indicate cannot be subscribed."""
    holder: dict[str, FakeTransport] = {}
    client, _scanner = await _connect(holder, make_responder(0x02))  # read only
    with pytest.raises(BleakError, match="notify or indicate"):
        await client.start_notify(_char(client), lambda _d: None)


async def test_start_notify_rejects_missing_cccd() -> None:
    """A notifying characteristic with no CCCD descriptor is rejected."""
    holder: dict[str, FakeTransport] = {}
    client, _scanner = await _connect(holder, make_responder(has_cccd=False))
    with pytest.raises(BleakError, match="client configuration descriptor"):
        await client.start_notify(_char(client), lambda _d: None)


async def test_stop_notify_clears_cccd_and_handler() -> None:
    """stop_notify clears the CCCD and stops routing notifications."""
    holder: dict[str, FakeTransport] = {}
    client, _scanner = await _connect(holder)
    char = _char(client)
    received: list[bytearray] = []
    await client.start_notify(char, received.append)
    await client.stop_notify(char)
    assert holder["transport"].sent[-1] == b"\x12\x04\x00\x00\x00"  # CCCD off
    holder["transport"].inject(bytes([_NTF]) + (0x0003).to_bytes(2, "little") + b"\x09")
    assert received == []  # handler removed, notification dropped


async def test_start_notify_unwinds_handler_on_cccd_write_failure() -> None:
    """If the CCCD write fails, no notify handler is left registered."""
    holder: dict[str, FakeTransport] = {}
    client, _scanner = await _connect(holder, make_responder(fail_write=True))
    char = _char(client)
    received: list[bytearray] = []
    with pytest.raises(BleakError):
        await client.start_notify(char, received.append)
    # The handler was unwound, so a stray notification is not delivered.
    holder["transport"].inject(bytes([_NTF]) + (0x0003).to_bytes(2, "little") + b"\x09")
    assert received == []


async def test_stop_notify_without_cccd_only_drops_handler() -> None:
    """stop_notify on a characteristic with no CCCD just drops the handler."""
    holder: dict[str, FakeTransport] = {}
    client, _scanner = await _connect(holder, make_responder(has_cccd=False))
    char = _char(client)
    before = len(holder["transport"].sent)
    await client.stop_notify(char)
    assert len(holder["transport"].sent) == before  # no CCCD write attempted


# -- disconnect / teardown ------------------------------------------------
async def test_disconnect_closes_transport() -> None:
    """Disconnect closes the socket and reports not connected."""
    holder: dict[str, FakeTransport] = {}
    client, _scanner = await _connect(holder)
    await client.disconnect()
    assert holder["transport"].closed is True
    assert client.is_connected is False


async def test_disconnect_fires_disconnected_callback() -> None:
    """A deliberate disconnect fires the callback, matching the BlueZ backend."""
    holder: dict[str, FakeTransport] = {}
    fired: list[bool] = []
    client, _scanner = await _connect(
        holder, disconnected_callback=lambda: fired.append(True)
    )
    await client.disconnect()
    assert fired == [True]


async def test_unexpected_drop_fires_disconnected_callback() -> None:
    """A transport drop tears down the client and fires the bleak callback."""
    holder: dict[str, FakeTransport] = {}
    fired: list[bool] = []
    client, _scanner = await _connect(
        holder, disconnected_callback=lambda: fired.append(True)
    )
    holder["transport"].drop(OSError("reset"))
    assert fired == [True]
    assert client.is_connected is False


async def test_operations_raise_after_disconnect() -> None:
    """GATT operations on a disconnected client raise a BleakError."""
    holder: dict[str, FakeTransport] = {}
    client, _scanner = await _connect(holder)
    char = _char(client)
    await client.disconnect()
    with pytest.raises(BleakError, match="not connected"):
        await client.read_gatt_char(char)


async def test_pair_and_unpair_not_supported() -> None:
    """Pairing is not implemented in this client yet."""
    client, _scanner = _make_client()
    with pytest.raises(NotImplementedError, match="pairing"):
        await client.pair()
    with pytest.raises(NotImplementedError, match="unpairing"):
        await client.unpair()


async def test_mtu_size_default_before_connect() -> None:
    """Before connect the MTU reports the ATT default."""
    client, _scanner = _make_client()
    assert client.mtu_size == 23
