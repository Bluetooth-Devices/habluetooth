"""
ATT protocol client and GATT discovery for the kernel/L2CAP backend.

This is the pure ATT/GATT logic for talking to a peripheral over an L2CAP ATT
channel: it encodes ATT requests, decodes responses, and drives the central
(client) subset of GATT, namely MTU exchange, primary-service / characteristic
/ descriptor discovery, reads (with long-read continuation), writes (with and
without response), and notification / indication handling.

It is deliberately transport-agnostic. Outbound PDUs are written through the
``send`` coroutine supplied at construction, and inbound PDUs are delivered by
the transport via :meth:`ATTClient.data_received`. Nothing here opens a socket,
so the whole module is unit-testable without Bluetooth hardware. The L2CAP
socket that feeds it lands in a later change.

ATT is strictly sequential: at most one request is outstanding at a time, so a
single transaction lock plus one pending future is sufficient. Notifications
and indications are demultiplexed out of the same inbound stream by opcode.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bleak import BleakError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

_LOGGER = logging.getLogger(__name__)

# -- ATT opcodes ----------------------------------------------------------
ATT_ERROR_RSP = 0x01
ATT_EXCHANGE_MTU_REQ = 0x02
ATT_EXCHANGE_MTU_RSP = 0x03
ATT_FIND_INFO_REQ = 0x04
ATT_FIND_INFO_RSP = 0x05
ATT_READ_BY_TYPE_REQ = 0x08
ATT_READ_BY_TYPE_RSP = 0x09
ATT_READ_REQ = 0x0A
ATT_READ_RSP = 0x0B
ATT_READ_BLOB_REQ = 0x0C
ATT_READ_BLOB_RSP = 0x0D
ATT_READ_BY_GROUP_TYPE_REQ = 0x10
ATT_READ_BY_GROUP_TYPE_RSP = 0x11
ATT_WRITE_REQ = 0x12
ATT_WRITE_RSP = 0x13
ATT_WRITE_CMD = 0x52
ATT_HANDLE_VALUE_NTF = 0x1B
ATT_HANDLE_VALUE_IND = 0x1D
ATT_HANDLE_VALUE_CFM = 0x1E

# -- ATT error codes ------------------------------------------------------
ATT_ERR_READ_NOT_PERMITTED = 0x02
ATT_ERR_INSUFFICIENT_AUTHENTICATION = 0x05
ATT_ERR_REQUEST_NOT_SUPPORTED = 0x06
ATT_ERR_INVALID_OFFSET = 0x07
ATT_ERR_ATTRIBUTE_NOT_FOUND = 0x0A
ATT_ERR_ATTRIBUTE_NOT_LONG = 0x0B
ATT_ERR_INSUFFICIENT_ENCRYPTION = 0x0F

# -- GATT well-known attribute types (16-bit, little-endian on the wire) --
GATT_PRIMARY_SERVICE = b"\x00\x28"  # 0x2800
GATT_CHARACTERISTIC = b"\x03\x28"  # 0x2803
CCCD_UUID = "00002902-0000-1000-8000-00805f9b34fb"

# -- characteristic property flags (Core Vol 3 Part G 3.3.1.1) ------------
CHAR_PROP_BROADCAST = 0x01
CHAR_PROP_READ = 0x02
CHAR_PROP_WRITE_NO_RESPONSE = 0x04
CHAR_PROP_WRITE = 0x08
CHAR_PROP_NOTIFY = 0x10
CHAR_PROP_INDICATE = 0x20
CHAR_PROP_AUTH_SIGNED_WRITES = 0x40
CHAR_PROP_EXTENDED = 0x80

_PROPERTY_MAP: list[tuple[int, str]] = [
    (CHAR_PROP_BROADCAST, "broadcast"),
    (CHAR_PROP_READ, "read"),
    (CHAR_PROP_WRITE_NO_RESPONSE, "write-without-response"),
    (CHAR_PROP_WRITE, "write"),
    (CHAR_PROP_NOTIFY, "notify"),
    (CHAR_PROP_INDICATE, "indicate"),
    (CHAR_PROP_AUTH_SIGNED_WRITES, "authenticated-signed-writes"),
    (CHAR_PROP_EXTENDED, "extended-properties"),
]

DEFAULT_MTU = 23
PREFERRED_MTU = 247
ATT_TIMEOUT = 30.0

_BASE_UUID_SUFFIX = "-0000-1000-8000-00805f9b34fb"

_ERROR_RSP = struct.Struct("<BHB")


def properties_to_strings(properties: int) -> list[str]:
    """Expand a characteristic property bitmask into bleak property names."""
    return [name for flag, name in _PROPERTY_MAP if properties & flag]


def uuid_from_bytes(data: bytes) -> str:
    """
    Convert a little-endian ATT UUID (2 or 16 bytes) to a string.

    16-bit UUIDs are expanded against the Bluetooth base UUID so callers always
    receive a full 128-bit UUID string. Any other length is a malformed PDU and
    raises rather than producing a bogus UUID.
    """
    if len(data) == 2:
        return f"0000{int.from_bytes(data, 'little'):04x}{_BASE_UUID_SUFFIX}"
    if len(data) == 16:
        # 128-bit UUID, stored little-endian on the wire.
        hexs = data[::-1].hex()
        return f"{hexs[0:8]}-{hexs[8:12]}-{hexs[12:16]}-{hexs[16:20]}-{hexs[20:32]}"
    msg = f"invalid ATT UUID length {len(data)}"
    raise BleakError(msg)


class ATTError(BleakError):
    """An ATT error response from the peer."""

    def __init__(self, req_opcode: int, handle: int, error_code: int) -> None:
        """Record the request opcode, handle, and ATT error code."""
        self.req_opcode = req_opcode
        self.handle = handle
        self.error_code = error_code
        super().__init__(
            f"ATT error 0x{error_code:02x} "
            f"(req 0x{req_opcode:02x}, handle 0x{handle:04x})"
        )


# -- GATT model -----------------------------------------------------------
@dataclass(slots=True)
class GattDescriptor:
    """A discovered GATT descriptor."""

    handle: int
    uuid: str


@dataclass(slots=True)
class GattCharacteristic:
    """A discovered GATT characteristic and its descriptors."""

    handle: int  # declaration handle
    value_handle: int
    uuid: str
    properties: int
    end_handle: int = 0
    descriptors: list[GattDescriptor] = field(default_factory=list)


@dataclass(slots=True)
class GattService:
    """A discovered GATT primary service and its characteristics."""

    handle: int
    end_handle: int
    uuid: str
    characteristics: list[GattCharacteristic] = field(default_factory=list)


class ATTClient:
    """
    An ATT client driven over a transport-agnostic byte stream.

    ``send`` is awaited to write one ATT PDU; the transport must deliver each
    inbound PDU to :meth:`data_received` and report loss via
    :meth:`connection_lost`.

    ATT requests are serialized by a transaction lock, but write commands and
    indication confirmations are sent independently of an outstanding request
    (as ATT permits), so ``send`` may be called concurrently. Each call carries
    exactly one PDU, so on a datagram transport (an L2CAP ``SOCK_SEQPACKET``
    socket) the writes stay framed; the injected ``send`` must therefore be safe
    to call concurrently and write each PDU atomically.
    """

    def __init__(
        self,
        send: Callable[[bytes], Awaitable[None]],
        on_disconnect: Callable[[Exception | None], None] | None = None,
    ) -> None:
        """Bind the client to a transport ``send`` coroutine."""
        self._send = send
        self._on_disconnect = on_disconnect
        self._txn_lock = asyncio.Lock()
        self._pending: asyncio.Future[bytes] | None = None
        self._mtu = DEFAULT_MTU
        self._notify_handlers: dict[int, Callable[[bytearray], None]] = {}
        # Strong references to fire-and-forget confirm tasks so they cannot be
        # garbage-collected before they run.
        self._background_tasks: set[asyncio.Task[None]] = set()
        # Set once the channel can no longer be trusted (disconnect or a request
        # timeout, after which a late response could be misattributed to a new
        # request since ATT has no transaction id).
        self._closed = False

    @property
    def mtu(self) -> int:
        """Return the negotiated ATT MTU (23 until exchanged)."""
        return self._mtu

    # -- inbound demux -----------------------------------------------------
    def data_received(self, data: bytes) -> None:
        """Demultiplex one inbound ATT PDU into a notify or a response."""
        if not data:
            return
        if self._closed:
            # The channel has been poisoned (e.g. a request timed out) or torn
            # down; the transport may still be open but is no longer trusted, so
            # drop late inbound PDUs rather than delivering a stale notification
            # or confirming an indication on a channel we are abandoning.
            return
        opcode = data[0]
        if opcode in (ATT_HANDLE_VALUE_NTF, ATT_HANDLE_VALUE_IND):
            # opcode (1) + handle (2); a shorter frame is malformed, so drop it
            # rather than fabricating a handle and dispatching an empty value.
            if len(data) < 3:
                _LOGGER.debug(
                    "Dropping truncated ATT handle-value PDU 0x%02x (%d bytes)",
                    opcode,
                    len(data),
                )
                return
            self._dispatch_notify(int.from_bytes(data[1:3], "little"), data[3:])
            if opcode == ATT_HANDLE_VALUE_IND:
                # Acknowledge the indication; retain a strong reference so the
                # task is not garbage-collected before it runs, and surface any
                # send failure via the done callback rather than as an
                # unretrieved task exception.
                task = asyncio.get_running_loop().create_task(self._confirm())
                self._background_tasks.add(task)
                task.add_done_callback(self._on_confirm_done)
            return
        if (fut := self._pending) is not None and not fut.done():
            fut.set_result(bytes(data))
        else:
            _LOGGER.debug("Dropping ATT PDU 0x%02x with no pending transaction", opcode)

    def connection_lost(self, exc: Exception | None) -> None:
        """Fail any in-flight transaction and notify the disconnect callback."""
        if (fut := self._pending) is not None and not fut.done():
            fut.set_exception(exc or BleakError("disconnected"))
        self._poison(exc)

    def _poison(self, exc: Exception | None) -> None:
        """Close the channel and notify the disconnect callback once."""
        if self._closed:
            return
        self._closed = True
        if self._on_disconnect is not None:
            self._on_disconnect(exc)

    async def _confirm(self) -> None:
        await self._send(bytes([ATT_HANDLE_VALUE_CFM]))

    def _on_confirm_done(self, task: asyncio.Task[None]) -> None:
        self._background_tasks.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:
            # A CFM send only fails when the transport is already failing, in
            # which case connection_lost fires and poisons the channel; this is
            # the supplementary detail, so debug is deliberate, not an oversight.
            _LOGGER.debug("Failed to confirm ATT indication: %s", exc)

    def _dispatch_notify(self, value_handle: int, value: bytes) -> None:
        if (cb := self._notify_handlers.get(value_handle)) is None:
            _LOGGER.debug(
                "Notification for unhandled value handle 0x%04x dropped", value_handle
            )
            return
        # Isolate the subscriber: data_received runs in the transport read loop,
        # so a raising callback must not tear down inbound demux for the whole
        # connection (and for indications it would also skip the confirmation).
        try:
            cb(bytearray(value))
        except Exception:
            _LOGGER.exception(
                "Error in notification handler for value handle 0x%04x", value_handle
            )

    # -- transactions ------------------------------------------------------
    def _raise_if_closed(self) -> None:
        if self._closed:
            msg = "ATT channel closed"
            raise BleakError(msg)

    async def _request(self, payload: bytes, expected_opcode: int) -> bytes:
        self._raise_if_closed()
        async with self._txn_lock:
            fut: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()
            self._pending = fut
            try:
                await self._send(payload)
                data = await asyncio.wait_for(fut, ATT_TIMEOUT)
            except TimeoutError as exc:
                # The timed-out request may still be in flight; ATT has no
                # transaction id, so a late response could be matched to the
                # next request. Poison the channel so nothing else is sent.
                self._poison(exc)
                msg = "ATT request timed out"
                raise BleakError(msg) from exc
            finally:
                self._pending = None
        opcode = data[0]
        if opcode == ATT_ERROR_RSP:
            if len(data) < 5:
                msg = "truncated ATT error response"
                raise BleakError(msg)
            req_op, handle, err = _ERROR_RSP.unpack_from(data, 1)
            raise ATTError(req_op, handle, err)
        if opcode != expected_opcode:
            msg = (
                f"unexpected ATT opcode 0x{opcode:02x} (wanted 0x{expected_opcode:02x})"
            )
            raise BleakError(msg)
        return data

    async def exchange_mtu(self, mtu: int = PREFERRED_MTU) -> int:
        """Negotiate the ATT MTU and return the agreed value."""
        payload = bytes([ATT_EXCHANGE_MTU_REQ]) + mtu.to_bytes(2, "little")
        try:
            data = await self._request(payload, ATT_EXCHANGE_MTU_RSP)
        except ATTError as err:
            # Only a peer that does not implement MTU exchange is a benign
            # fallback to the default MTU; surface any other error (e.g. an
            # encryption/authentication rejection) rather than reporting it as a
            # successful negotiation.
            if err.error_code != ATT_ERR_REQUEST_NOT_SUPPORTED:
                raise
            _LOGGER.debug(
                "Peer does not support MTU exchange; keeping default MTU %d",
                self._mtu,
            )
            return self._mtu
        if len(data) < 3:
            msg = "truncated ATT MTU response"
            raise BleakError(msg)
        server_mtu = int.from_bytes(data[1:3], "little")
        self._mtu = max(DEFAULT_MTU, min(mtu, server_mtu))
        return self._mtu

    # -- reads -------------------------------------------------------------
    async def read(self, handle: int) -> bytes:
        """Read an attribute value, continuing with Read Blob if it is long."""
        data = await self._request(
            bytes([ATT_READ_REQ]) + handle.to_bytes(2, "little"), ATT_READ_RSP
        )
        value = bytearray(data[1:])
        # A chunk that exactly fills the PDU may be truncated, so continue with
        # Read Blob; a short chunk means we have the whole value. Keying on the
        # last chunk (not the running total) avoids an extra round-trip on a
        # value whose length happens to be a multiple of the chunk size.
        while len(value) >= self._mtu - 1:
            # The Read Blob offset is a 16-bit field, so a value cannot extend
            # past 0xFFFF; stop with a clear error rather than letting a peer
            # that always returns a full chunk overflow offset.to_bytes(2).
            if len(value) > 0xFFFF:
                msg = "ATT value exceeds the 16-bit Read Blob offset range"
                raise BleakError(msg)
            blob = await self._read_blob(handle, len(value))
            value += blob
            if len(blob) < self._mtu - 1:
                break
        return bytes(value)

    async def _read_blob(self, handle: int, offset: int) -> bytes:
        payload = (
            bytes([ATT_READ_BLOB_REQ])
            + handle.to_bytes(2, "little")
            + offset.to_bytes(2, "little")
        )
        try:
            data = await self._request(payload, ATT_READ_BLOB_RSP)
        except ATTError as err:
            if err.error_code in (ATT_ERR_INVALID_OFFSET, ATT_ERR_ATTRIBUTE_NOT_LONG):
                # Normal end of a long read (a blob read at offset == length
                # returns INVALID_OFFSET); logged for field observability.
                _LOGGER.debug(
                    "Read Blob for handle 0x%04x ended at offset %d: error 0x%02x",
                    handle,
                    offset,
                    err.error_code,
                )
                return b""
            raise
        return data[1:]

    # -- writes ------------------------------------------------------------
    def _check_write_length(self, value: bytes) -> None:
        # An ATT write carries at most MTU - 3 bytes of value; longer values
        # need the Prepare/Execute Write (long write) procedure, which this
        # codec does not implement, so reject them explicitly rather than emit
        # an over-MTU PDU the transport or peer will opaquely fail.
        if len(value) > self._mtu - 3:
            msg = (
                f"value too long for an ATT write: {len(value)} > {self._mtu - 3} "
                "(long writes are not supported)"
            )
            raise BleakError(msg)

    async def write(self, handle: int, value: bytes) -> None:
        """
        Write an attribute value with a response (Write Request).

        Values longer than ``mtu - 3`` are rejected; long writes are not
        supported.
        """
        self._check_write_length(value)
        payload = bytes([ATT_WRITE_REQ]) + handle.to_bytes(2, "little") + bytes(value)
        await self._request(payload, ATT_WRITE_RSP)

    async def write_command(self, handle: int, value: bytes) -> None:
        """
        Write an attribute value without a response (Write Command).

        Values longer than ``mtu - 3`` are rejected; long writes are not
        supported.
        """
        self._raise_if_closed()
        self._check_write_length(value)
        payload = bytes([ATT_WRITE_CMD]) + handle.to_bytes(2, "little") + bytes(value)
        await self._send(payload)

    # -- discovery primitives ---------------------------------------------
    async def read_by_group_type(
        self, start: int, end: int, group_uuid: bytes
    ) -> list[tuple[int, int, bytes]]:
        """Enumerate grouping attributes (e.g. primary services) in a range."""
        results: list[tuple[int, int, bytes]] = []
        while start <= end:
            payload = (
                bytes([ATT_READ_BY_GROUP_TYPE_REQ])
                + start.to_bytes(2, "little")
                + end.to_bytes(2, "little")
                + group_uuid
            )
            try:
                data = await self._request(payload, ATT_READ_BY_GROUP_TYPE_RSP)
            except ATTError as err:
                if err.error_code == ATT_ERR_ATTRIBUTE_NOT_FOUND:
                    break
                raise
            # Each entry is handle(2) + end_handle(2) + value; reject an
            # untrusted peer's malformed stride before it crashes range().
            if len(data) < 2 or (length := data[1]) < 4:
                msg = "malformed read-by-group-type response"
                raise BleakError(msg)
            entries = data[2:]
            # A real server returns ATTRIBUTE_NOT_FOUND when nothing remains, so
            # a response carrying no full entry is malformed; reject it (like the
            # bad-stride case) rather than re-requesting and amplifying or
            # silently returning a partial discovery tree. A body that is not a
            # whole multiple of the stride is malformed for the same reason; a
            # trailing partial entry is rejected rather than silently dropped.
            if len(entries) < length or len(entries) % length:
                msg = "malformed read-by-group-type response (truncated entries)"
                raise BleakError(msg)
            last = start
            for i in range(0, len(entries), length):
                entry = entries[i : i + length]
                handle = int.from_bytes(entry[0:2], "little")
                end_handle = int.from_bytes(entry[2:4], "little")
                results.append((handle, end_handle, bytes(entry[4:length])))
                last = end_handle
            # last >= start here, so the cursor strictly advances each round and
            # the loop is bounded by the handle space. An entry whose handle is
            # exactly the cursor is legitimate for sparse consecutive attributes
            # (the first match can sit at start), so we accept the bounded
            # worst case of one entry per round rather than truncate valid
            # discovery; the entry-less guard above already stops zero progress.
            if last >= 0xFFFF or last < start:
                break
            start = last + 1
        return results

    async def read_by_type(
        self, start: int, end: int, type_uuid: bytes
    ) -> list[tuple[int, bytes]]:
        """Enumerate attributes of a given type (e.g. characteristics)."""
        results: list[tuple[int, bytes]] = []
        while start <= end:
            payload = (
                bytes([ATT_READ_BY_TYPE_REQ])
                + start.to_bytes(2, "little")
                + end.to_bytes(2, "little")
                + type_uuid
            )
            try:
                data = await self._request(payload, ATT_READ_BY_TYPE_RSP)
            except ATTError as err:
                if err.error_code == ATT_ERR_ATTRIBUTE_NOT_FOUND:
                    break
                raise
            # Each entry is handle(2) + value; reject an untrusted peer's
            # malformed stride before it crashes range().
            if len(data) < 2 or (length := data[1]) < 2:
                msg = "malformed read-by-type response"
                raise BleakError(msg)
            entries = data[2:]
            # An entry-less response is malformed (a real server sends
            # ATTRIBUTE_NOT_FOUND); a body that is not a whole multiple of the
            # stride is malformed too. Reject both rather than re-requesting or
            # silently dropping a trailing partial entry.
            if len(entries) < length or len(entries) % length:
                msg = "malformed read-by-type response (truncated entries)"
                raise BleakError(msg)
            last = start
            for i in range(0, len(entries), length):
                entry = entries[i : i + length]
                handle = int.from_bytes(entry[0:2], "little")
                results.append((handle, bytes(entry[2:length])))
                last = handle
            if last >= 0xFFFF or last < start:
                break
            start = last + 1
        return results

    async def find_information(self, start: int, end: int) -> list[tuple[int, str]]:
        """Enumerate attribute handles and UUIDs (used for descriptors)."""
        results: list[tuple[int, str]] = []
        while start <= end:
            payload = (
                bytes([ATT_FIND_INFO_REQ])
                + start.to_bytes(2, "little")
                + end.to_bytes(2, "little")
            )
            try:
                data = await self._request(payload, ATT_FIND_INFO_RSP)
            except ATTError as err:
                if err.error_code == ATT_ERR_ATTRIBUTE_NOT_FOUND:
                    break
                raise
            # The format byte is 0x01 (16-bit) or 0x02 (128-bit); anything else
            # is a malformed PDU and must not be mis-parsed as a 128-bit UUID.
            fmt = data[1] if len(data) >= 2 else 0
            if fmt == 0x01:
                uuid_len = 2
            elif fmt == 0x02:
                uuid_len = 16
            else:
                msg = f"invalid find-information format 0x{fmt:02x}"
                raise BleakError(msg)
            entry_len = 2 + uuid_len
            entries = data[2:]
            # An entry-less response is malformed (a real server sends
            # ATTRIBUTE_NOT_FOUND); a body that is not a whole multiple of the
            # entry size is malformed too. Reject both rather than re-requesting
            # or silently dropping a trailing partial entry.
            if len(entries) < entry_len or len(entries) % entry_len:
                msg = "malformed find-information response (truncated entries)"
                raise BleakError(msg)
            last = start
            for i in range(0, len(entries), entry_len):
                entry = entries[i : i + entry_len]
                handle = int.from_bytes(entry[0:2], "little")
                results.append((handle, uuid_from_bytes(entry[2:entry_len])))
                last = handle
            if last >= 0xFFFF or last < start:
                break
            start = last + 1
        return results

    # -- notifications -----------------------------------------------------
    def set_notify_handler(
        self, value_handle: int, callback: Callable[[bytearray], None]
    ) -> None:
        """Route notifications/indications for a value handle to ``callback``."""
        self._notify_handlers[value_handle] = callback

    def remove_notify_handler(self, value_handle: int) -> None:
        """Stop routing notifications for a value handle."""
        self._notify_handlers.pop(value_handle, None)

    # -- high-level GATT discovery ----------------------------------------
    async def discover(self) -> list[GattService]:
        """Discover the full primary-service / characteristic / descriptor tree."""
        services = [
            GattService(handle, end_handle, uuid_from_bytes(value))
            for handle, end_handle, value in await self.read_by_group_type(
                0x0001, 0xFFFF, GATT_PRIMARY_SERVICE
            )
        ]
        for svc in services:
            chars: list[GattCharacteristic] = []
            for decl_handle, value in await self.read_by_type(
                svc.handle, svc.end_handle, GATT_CHARACTERISTIC
            ):
                # A declaration value is properties(1) + value_handle(2) +
                # uuid(2|16); reject a malformed peer's short declaration rather
                # than indexing past the end of it.
                if len(value) < 5:
                    msg = "malformed characteristic declaration"
                    raise BleakError(msg)
                chars.append(
                    GattCharacteristic(
                        decl_handle,
                        int.from_bytes(value[1:3], "little"),
                        uuid_from_bytes(value[3:]),
                        value[0],
                    )
                )
            # Each characteristic ends just before the next one (or the service
            # end); descriptors live between the value handle and that end.
            for i, char in enumerate(chars):
                char.end_handle = (
                    chars[i + 1].handle - 1 if i + 1 < len(chars) else svc.end_handle
                )
                if char.end_handle > char.value_handle:
                    char.descriptors = [
                        GattDescriptor(handle, uuid)
                        for handle, uuid in await self.find_information(
                            char.value_handle + 1, char.end_handle
                        )
                    ]
            svc.characteristics = chars
        return services
