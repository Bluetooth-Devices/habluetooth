"""Tests for the kernel/L2CAP GATT client backend."""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from bleak import BleakError
from bleak.backends.device import BLEDevice

from habluetooth.channels.att import (
    ATT_ERR_INSUFFICIENT_AUTHENTICATION,
    ATT_ERR_INSUFFICIENT_ENCRYPTION,
)
from habluetooth.channels.bluez import (
    AuthenticationFailed,
    LongTermKey,
    NewLongTermKey,
    UserConfirmationRequest,
    UserPasskeyRequest,
)
from habluetooth.channels.l2cap import (
    BT_SECURITY_HIGH,
    BT_SECURITY_LOW,
    BT_SECURITY_MEDIUM,
)
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
        self.security_level = BT_SECURITY_LOW
        self.security_requests: list[int] = []

    def set_security_level(self, level: int) -> bool:
        """Raise the tracked security level, never lowering it."""
        self.security_requests.append(level)
        if level <= self.security_level:
            return False
        self.security_level = level
        return True

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


async def test_connect_pair_true_without_mgmt_skips_bond() -> None:
    """connect(pair=True) with no mgmt wired still connects, skipping the bond."""
    holder: dict[str, FakeTransport] = {}
    client, _scanner = _make_client()  # no mgmt wired
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


# -- security escalation --------------------------------------------------
async def test_read_escalates_security_then_succeeds() -> None:
    """A read rejected for encryption raises link security and retries once."""
    holder: dict[str, FakeTransport] = {}
    base = make_responder()
    attempts = 0

    def responder(req: bytes) -> bytes | None:
        nonlocal attempts
        if req[0] == 0x0A:  # READ_REQ
            attempts += 1
            if attempts == 1:
                handle = int.from_bytes(req[1:3], "little")
                return _error(0x0A, handle, ATT_ERR_INSUFFICIENT_ENCRYPTION)
        return base(req)

    client, _scanner = await _connect(holder, responder)
    assert await client.read_gatt_char(_char(client)) == bytearray(_VALUE)
    assert holder["transport"].security_requests == [BT_SECURITY_MEDIUM]
    assert holder["transport"].security_level == BT_SECURITY_MEDIUM


async def test_escalate_security_steps_up_on_authentication() -> None:
    """Insufficient authentication steps the level up one, capped at HIGH."""
    client, _scanner = _make_client()
    transport = FakeTransport(make_responder(), lambda _d: None, lambda _e: None)
    transport.security_level = BT_SECURITY_MEDIUM
    client._sock = transport  # type: ignore[assignment]
    assert client._escalate_security(ATT_ERR_INSUFFICIENT_AUTHENTICATION) is True
    assert transport.security_level == BT_SECURITY_HIGH
    # The kernel exposes nothing above HIGH, so a further step declines.
    assert client._escalate_security(ATT_ERR_INSUFFICIENT_AUTHENTICATION) is False
    assert transport.security_level == BT_SECURITY_HIGH


async def test_escalate_security_declines_when_already_encrypted() -> None:
    """An encryption error at MEDIUM cannot be satisfied by encryption again."""
    client, _scanner = _make_client()
    transport = FakeTransport(make_responder(), lambda _d: None, lambda _e: None)
    transport.security_level = BT_SECURITY_MEDIUM
    client._sock = transport  # type: ignore[assignment]
    assert client._escalate_security(ATT_ERR_INSUFFICIENT_ENCRYPTION) is False
    assert transport.security_level == BT_SECURITY_MEDIUM


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


async def test_connection_register_callbacks_fire() -> None:
    """The client reports connect/disconnect to the scanner's slot callbacks."""
    holder: dict[str, FakeTransport] = {}
    registered: list[str] = []
    unregistered: list[str] = []
    client = HaMgmtClient(
        _ble_device(),
        client_data=MgmtClientData(
            adapter_address=_ADAPTER,
            scanner=FakeScanner(),
            register_connection=registered.append,
            unregister_connection=unregistered.append,
        ),
        timeout=5.0,
    )
    with _patch_transport(make_responder(), holder):
        await client.connect(False)
    assert registered == [_PEER]
    assert unregistered == []
    await client.disconnect()
    assert unregistered == [_PEER]


async def test_register_callback_failure_does_not_break_connect() -> None:
    """A raising slot register callback is swallowed; the connection survives."""
    holder: dict[str, FakeTransport] = {}

    def boom(_address: str) -> None:
        msg = "bookkeeping broke"
        raise RuntimeError(msg)

    client = HaMgmtClient(
        _ble_device(),
        client_data=MgmtClientData(
            adapter_address=_ADAPTER,
            scanner=FakeScanner(),
            register_connection=boom,
        ),
        timeout=5.0,
    )
    with _patch_transport(make_responder(), holder):
        await client.connect(False)
    assert client.is_connected is True


async def test_unregister_callback_failure_still_fires_disconnect() -> None:
    """A raising slot unregister callback does not block the disconnect callback."""
    holder: dict[str, FakeTransport] = {}
    fired: list[bool] = []

    def boom(_address: str) -> None:
        msg = "bookkeeping broke"
        raise RuntimeError(msg)

    client = HaMgmtClient(
        _ble_device(),
        client_data=MgmtClientData(
            adapter_address=_ADAPTER,
            scanner=FakeScanner(),
            unregister_connection=boom,
        ),
        timeout=5.0,
        disconnected_callback=lambda: fired.append(True),
    )
    with _patch_transport(make_responder(), holder):
        await client.connect(False)
    holder["transport"].drop(OSError("reset"))
    assert fired == [True]
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


async def test_unexpected_drop_logs_cause(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unexpected drop logs the originating cause for diagnosability."""
    holder: dict[str, FakeTransport] = {}
    _client, _scanner = await _connect(holder)
    with caplog.at_level(logging.DEBUG, logger="habluetooth.client_mgmt"):
        holder["transport"].drop(OSError("connection reset by peer"))
    assert "channel lost" in caplog.text
    assert "connection reset by peer" in caplog.text


async def test_operations_raise_after_disconnect() -> None:
    """GATT operations on a disconnected client raise a BleakError."""
    holder: dict[str, FakeTransport] = {}
    client, _scanner = await _connect(holder)
    char = _char(client)
    await client.disconnect()
    with pytest.raises(BleakError, match="not connected"):
        await client.read_gatt_char(char)


class FakeMgmt:
    """Records the pairing/bond mgmt commands the client issues."""

    def __init__(
        self, *, pair_ok: bool = True, load_ok: bool = True, unpair_ok: bool = True
    ) -> None:
        self.pair_ok = pair_ok
        self.load_ok = load_ok
        self.unpair_ok = unpair_ok
        self.paired: list[tuple[int, str, int]] = []
        self.unpaired: list[tuple[int, str]] = []
        self.loaded: list[tuple[int, list[LongTermKey]]] = []
        self.confirmations: list[tuple[int, str, int, bool]] = []
        self.confirm_raises = False
        self.event_to_emit: object | None = None  # delivered during pair_device
        self._handlers: dict[tuple[int, str], Callable[[object], None]] = {}

    async def user_confirmation_reply(
        self, idx: int, address: str, address_type: int, *, accept: bool = True
    ) -> bool:
        if self.confirm_raises:
            msg = "reply failed"
            raise OSError(msg)
        self.confirmations.append((idx, address, address_type, accept))
        return True

    def register_pairing_handler(
        self, idx: int, address: str, handler: Callable[[object], None]
    ) -> Callable[[], None]:
        self._handlers[(idx, address)] = handler

        def _unregister() -> None:
            self._handlers.pop((idx, address), None)

        return _unregister

    async def pair_device(
        self, idx: int, address: str, address_type: int, *_a: object
    ) -> bool:
        self.paired.append((idx, address, address_type))
        if self.event_to_emit is not None:
            # Simulate the kernel pushing a pairing event during pairing.
            self._handlers[(idx, address)](self.event_to_emit)
        return self.pair_ok

    async def unpair_device(
        self, idx: int, address: str, address_type: int, **_k: object
    ) -> bool:
        self.unpaired.append((idx, address))
        return self.unpair_ok

    async def load_long_term_keys(self, idx: int, keys: list[LongTermKey]) -> bool:
        self.loaded.append((idx, list(keys)))
        return self.load_ok


class _Store:
    """An in-memory per-adapter long-term-key store for tests."""

    def __init__(self, keys: list[LongTermKey] | None = None) -> None:
        self.keys: list[LongTermKey] = list(keys or [])

    def all(self) -> list[LongTermKey]:
        return list(self.keys)

    def add(self, key: LongTermKey) -> None:
        self.keys.append(key)

    def forget(self, address: str) -> None:
        self.keys = [key for key in self.keys if key.address != address]


def _ltk(address: str = _PEER) -> LongTermKey:
    return LongTermKey(
        address, BDADDR_LE_RANDOM, 0x05, False, 16, 0x1234, bytes(8), bytes(16)
    )


def _make_pairing_client(
    mgmt: FakeMgmt,
    store: _Store | None,
    *,
    adapter_idx: int | None = 0,
) -> HaMgmtClient:
    return HaMgmtClient(
        _ble_device(),
        client_data=MgmtClientData(
            adapter_address=_ADAPTER,
            scanner=FakeScanner(),
            adapter_idx=adapter_idx,
            mgmt=mgmt,  # type: ignore[arg-type]
            get_long_term_keys=store.all if store else None,
            add_long_term_key=store.add if store else None,
            forget_long_term_keys=store.forget if store else None,
        ),
        timeout=5.0,
    )


async def test_pair_captures_long_term_key() -> None:
    """pair() issues PAIR_DEVICE and persists the captured key."""
    mgmt = FakeMgmt()
    mgmt.event_to_emit = NewLongTermKey(1, _ltk())
    store = _Store()
    client = _make_pairing_client(mgmt, store)
    await client.pair()
    assert mgmt.paired == [(0, _PEER, BDADDR_LE_RANDOM)]
    assert store.keys == [_ltk()]


async def test_pair_ignores_zero_store_hint() -> None:
    """A key the kernel asks not to persist (store_hint 0) is not stored."""
    mgmt = FakeMgmt()
    mgmt.event_to_emit = NewLongTermKey(0, _ltk())
    store = _Store()
    client = _make_pairing_client(mgmt, store)
    await client.pair()
    assert store.keys == []


async def test_pair_ignores_non_key_event() -> None:
    """An AUTHENTICATION_FAILED event during pairing stores no key."""
    mgmt = FakeMgmt(pair_ok=False)
    mgmt.event_to_emit = AuthenticationFailed(_PEER, BDADDR_LE_RANDOM, 0x05)
    store = _Store()
    client = _make_pairing_client(mgmt, store)
    with pytest.raises(BleakError, match="pairing failed"):
        await client.pair()
    assert store.keys == []


async def test_pair_warns_when_no_store_wired(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A captured key with no store wired is logged, not silently dropped."""
    mgmt = FakeMgmt()
    mgmt.event_to_emit = NewLongTermKey(1, _ltk())
    client = _make_pairing_client(mgmt, None)
    with caplog.at_level("WARNING", logger="habluetooth.client_mgmt"):
        await client.pair()
    assert "no key store" in caplog.text


async def test_pair_failure_raises() -> None:
    """A failed PAIR_DEVICE raises and captures no key."""
    mgmt = FakeMgmt(pair_ok=False)
    store = _Store()
    client = _make_pairing_client(mgmt, store)
    with pytest.raises(BleakError, match="pairing failed"):
        await client.pair()
    assert store.keys == []


async def test_pair_requires_mgmt() -> None:
    """Without the mgmt socket, pairing raises a clear error."""
    client, _scanner = _make_client()  # no mgmt/adapter_idx wired
    with pytest.raises(BleakError, match="requires the management socket"):
        await client.pair()
    with pytest.raises(BleakError, match="requires the management socket"):
        await client.unpair()


async def test_pair_auto_confirms_just_works() -> None:
    """A just-works confirm (confirm_hint set) is accepted so the bond proceeds."""
    mgmt = FakeMgmt()
    mgmt.event_to_emit = UserConfirmationRequest(_PEER, BDADDR_LE_RANDOM, 1, 0)
    client = _make_pairing_client(mgmt, _Store())
    await client.pair()
    # Let the scheduled reply task run.
    await asyncio.gather(*client._pairing_tasks)
    assert mgmt.confirmations == [(0, _PEER, BDADDR_LE_RANDOM, True)]


async def test_pair_rejects_numeric_comparison() -> None:
    """A real numeric comparison (confirm_hint 0) is rejected, not blindly confirmed."""
    mgmt = FakeMgmt()
    mgmt.event_to_emit = UserConfirmationRequest(_PEER, BDADDR_LE_RANDOM, 0, 123456)
    client = _make_pairing_client(mgmt, _Store())
    await client.pair()
    await asyncio.gather(*client._pairing_tasks)
    assert mgmt.confirmations == [(0, _PEER, BDADDR_LE_RANDOM, False)]


async def test_pair_logs_failed_confirmation_reply(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A confirmation reply that fails to send is logged, not lost."""
    mgmt = FakeMgmt()
    mgmt.confirm_raises = True
    mgmt.event_to_emit = UserConfirmationRequest(_PEER, BDADDR_LE_RANDOM, 0, 123456)
    client = _make_pairing_client(mgmt, _Store())
    with caplog.at_level(logging.DEBUG, logger="habluetooth.client_mgmt"):
        await client.pair()
        await asyncio.gather(*client._pairing_tasks, return_exceptions=True)
    assert "pairing reply task failed" in caplog.text


async def test_pair_ignores_passkey_request() -> None:
    """A passkey request, unsatisfiable with NoInputNoOutput, is simply ignored."""
    mgmt = FakeMgmt()
    mgmt.event_to_emit = UserPasskeyRequest(_PEER, BDADDR_LE_RANDOM)
    client = _make_pairing_client(mgmt, _Store())
    await client.pair()  # completes; the event is not acted on
    assert mgmt.confirmations == []


async def test_pair_fails_fast_on_auth_failed() -> None:
    """An AUTH_FAILED event ends pairing before the mgmt command resolves."""

    class _BlockingPairMgmt:
        """A mgmt stand-in whose pair_device blocks until released."""

        def __init__(self) -> None:
            self.started = asyncio.Event()
            self._release = asyncio.Event()
            self._handlers: dict[tuple[int, str], Callable[[object], None]] = {}

        def register_pairing_handler(
            self, idx: int, address: str, handler: Callable[[object], None]
        ) -> Callable[[], None]:
            self._handlers[(idx, address)] = handler

            def _unregister() -> None:
                self._handlers.pop((idx, address), None)

            return _unregister

        async def pair_device(
            self, idx: int, address: str, address_type: int, *_a: object
        ) -> bool:
            self.started.set()
            await self._release.wait()  # never released in this test
            return True

        def deliver(self, idx: int, address: str, event: object) -> None:
            self._handlers[(idx, address)](event)

    mgmt = _BlockingPairMgmt()
    client = _make_pairing_client(mgmt, _Store())  # type: ignore[arg-type]
    task = asyncio.create_task(client.pair())
    await mgmt.started.wait()
    mgmt.deliver(0, _PEER, AuthenticationFailed(_PEER, BDADDR_LE_RANDOM, 0x05))
    with pytest.raises(BleakError, match="auth status 0x05"):
        await task


async def test_connect_pair_true_bonds_when_mgmt_wired() -> None:
    """connect(pair=True) with mgmt wired bonds as part of connecting."""
    holder: dict[str, FakeTransport] = {}
    mgmt = FakeMgmt()
    mgmt.event_to_emit = NewLongTermKey(1, _ltk())
    store = _Store()
    client = _make_pairing_client(mgmt, store)
    with _patch_transport(make_responder(), holder):
        await client.connect(True)
    assert client.is_connected is True
    assert mgmt.paired == [(0, _PEER, BDADDR_LE_RANDOM)]
    assert store.keys == [_ltk()]


async def test_connect_pair_true_skips_rebond_when_already_bonded() -> None:
    """connect(pair=True) on an already-bonded peer does not re-pair."""
    holder: dict[str, FakeTransport] = {}
    mgmt = FakeMgmt()
    store = _Store([_ltk()])  # already bonded
    client = _make_pairing_client(mgmt, store)
    with _patch_transport(make_responder(), holder):
        await client.connect(True)
    assert client.is_connected is True
    assert mgmt.paired == []  # no redundant PAIR_DEVICE that would fail the connect


async def test_connect_encrypts_proactively_for_bonded_peer() -> None:
    """A reconnect with a stored key opens the socket at MEDIUM up front."""
    captured: dict[str, int] = {}
    mgmt = FakeMgmt()
    store = _Store([_ltk()])  # a bond already exists for this peer
    client = _make_pairing_client(mgmt, store)
    base = make_responder()

    async def fake_create_connection(
        *,
        on_data: Callable[[bytes], None],
        on_close: Callable[[Exception | None], None],
        security_level: int,
        **_kwargs: object,
    ) -> FakeTransport:
        captured["security_level"] = security_level
        transport = FakeTransport(base, on_data, on_close)
        transport.security_level = security_level
        return transport

    with patch(
        "habluetooth.client_mgmt.L2CAPSocket.create_connection", fake_create_connection
    ):
        await client.connect(False)
    assert captured["security_level"] == BT_SECURITY_MEDIUM


async def test_connect_unbonded_opens_low_security() -> None:
    """With no stored key, the socket opens at LOW and escalates on demand."""
    captured: dict[str, int] = {}
    mgmt = FakeMgmt()
    client = _make_pairing_client(mgmt, _Store())  # empty store
    base = make_responder()

    async def fake_create_connection(
        *,
        on_data: Callable[[bytes], None],
        on_close: Callable[[Exception | None], None],
        security_level: int,
        **_kwargs: object,
    ) -> FakeTransport:
        captured["security_level"] = security_level
        return FakeTransport(base, on_data, on_close)

    with patch(
        "habluetooth.client_mgmt.L2CAPSocket.create_connection", fake_create_connection
    ):
        await client.connect(False)
    assert captured["security_level"] == BT_SECURITY_LOW


async def test_unpair_removes_bond() -> None:
    """unpair() issues UNPAIR_DEVICE and forgets the stored key."""
    mgmt = FakeMgmt()
    store = _Store([_ltk()])
    client = _make_pairing_client(mgmt, store)
    await client.unpair()
    assert mgmt.unpaired == [(0, _PEER)]
    assert store.keys == []


async def test_unpair_without_store() -> None:
    """unpair() works when no key store was wired."""
    mgmt = FakeMgmt()
    client = _make_pairing_client(mgmt, None)
    await client.unpair()
    assert mgmt.unpaired == [(0, _PEER)]


async def test_unpair_failure_keeps_stored_key() -> None:
    """A failed UNPAIR_DEVICE raises and leaves the stored key in place."""
    mgmt = FakeMgmt(unpair_ok=False)
    store = _Store([_ltk()])
    client = _make_pairing_client(mgmt, store)
    with pytest.raises(BleakError, match="unpair failed"):
        await client.unpair()
    assert store.keys == [_ltk()]  # not desynced from the controller


async def test_connect_restores_all_adapter_bonds() -> None:
    """connect() reloads every stored key (LOAD replaces the whole list)."""
    holder: dict[str, FakeTransport] = {}
    mgmt = FakeMgmt()
    other = LongTermKey(
        "11:22:33:44:55:66",
        BDADDR_LE_RANDOM,
        0x05,
        True,
        16,
        0x9999,
        bytes(8),
        bytes(16),
    )
    store = _Store([_ltk(), other])
    client = _make_pairing_client(mgmt, store)
    with _patch_transport(make_responder(), holder):
        await client.connect(False)
    # All keys are sent, not just the connecting peer's, so other bonds survive.
    assert mgmt.loaded == [(0, [_ltk(), other])]


async def test_connect_warns_when_restore_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed key restore is logged rather than silently proceeding."""
    holder: dict[str, FakeTransport] = {}
    mgmt = FakeMgmt(load_ok=False)
    store = _Store([_ltk()])
    client = _make_pairing_client(mgmt, store)
    with (
        _patch_transport(make_responder(), holder),
        caplog.at_level("WARNING", logger="habluetooth.client_mgmt"),
    ):
        await client.connect(False)
    assert "failed to restore" in caplog.text


async def test_connect_without_bond_loads_nothing() -> None:
    """connect() does not load keys when nothing is bonded on the adapter."""
    holder: dict[str, FakeTransport] = {}
    mgmt = FakeMgmt()
    client = _make_pairing_client(mgmt, _Store())
    with _patch_transport(make_responder(), holder):
        await client.connect(False)
    assert mgmt.loaded == []


async def test_mtu_size_default_before_connect() -> None:
    """Before connect the MTU reports the ATT default."""
    client, _scanner = _make_client()
    assert client.mtu_size == 23


async def test_address_type_falls_back_to_public_when_missing() -> None:
    """A device without an address_type in details defaults to LE public."""
    scanner = FakeScanner()
    device = BLEDevice(_PEER, "test-device", {"source": _ADAPTER})  # no address_type
    client = HaMgmtClient(
        device,
        client_data=MgmtClientData(adapter_address=_ADAPTER, scanner=scanner),
        timeout=5.0,
    )
    assert client._address_type == 0x01  # BDADDR_LE_PUBLIC
