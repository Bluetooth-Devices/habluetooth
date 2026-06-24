"""Tests for the ATT/GATT codec."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from bleak import BleakError

from habluetooth.channels.att import (
    ATT_ERR_ATTRIBUTE_NOT_FOUND,
    ATT_ERR_INSUFFICIENT_AUTHENTICATION,
    ATT_ERR_INSUFFICIENT_ENCRYPTION,
    ATT_ERR_INVALID_OFFSET,
    ATT_ERR_READ_NOT_PERMITTED,
    ATT_ERR_REQUEST_NOT_SUPPORTED,
    ATT_ERROR_RSP,
    ATT_EXCHANGE_MTU_RSP,
    ATT_FIND_INFO_REQ,
    ATT_FIND_INFO_RSP,
    ATT_HANDLE_VALUE_CFM,
    ATT_HANDLE_VALUE_IND,
    ATT_HANDLE_VALUE_NTF,
    ATT_READ_BLOB_RSP,
    ATT_READ_BY_GROUP_TYPE_REQ,
    ATT_READ_BY_GROUP_TYPE_RSP,
    ATT_READ_BY_TYPE_REQ,
    ATT_READ_BY_TYPE_RSP,
    ATT_READ_RSP,
    ATT_WRITE_CMD,
    ATT_WRITE_RSP,
    CCCD_UUID,
    DEFAULT_MTU,
    ATTClient,
    ATTError,
    properties_to_strings,
    uuid_from_bytes,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

pytestmark = pytest.mark.asyncio


class FakePeer:
    """A fake ATT peer that captures sent PDUs and auto-replies."""

    def __init__(
        self,
        responder: Callable[[bytes], bytes | None] | None = None,
        *,
        escalate_security: Callable[[int], bool] | None = None,
    ) -> None:
        """Create the bound ATTClient and optionally script replies."""
        self.sent: list[bytes] = []
        self.responder = responder
        self.client = ATTClient(self.send, escalate_security=escalate_security)

    async def send(self, data: bytes) -> None:
        """Capture an outbound PDU and feed back the scripted response."""
        self.sent.append(bytes(data))
        if self.responder is not None:
            response = self.responder(bytes(data))
            if response is not None:
                self.client.data_received(response)


def _att_error(req_opcode: int, handle: int, code: int) -> bytes:
    return (
        bytes([ATT_ERROR_RSP, req_opcode])
        + handle.to_bytes(2, "little")
        + bytes([code])
    )


# -- pure helpers ---------------------------------------------------------
async def test_uuid_from_bytes_16_bit() -> None:
    """A 16-bit UUID expands against the Bluetooth base UUID."""
    assert uuid_from_bytes(b"\x02\x29") == CCCD_UUID
    assert uuid_from_bytes(b"\x0d\x18") == "0000180d-0000-1000-8000-00805f9b34fb"


async def test_uuid_from_bytes_128_bit() -> None:
    """A 128-bit UUID is byte-reversed and formatted."""
    raw = bytes.fromhex("fb349b5f8000008000100000661a0a05")
    assert uuid_from_bytes(raw) == "050a1a66-0000-1000-8000-00805f9b34fb"


async def test_properties_to_strings() -> None:
    """The property bitmask expands to bleak property names."""
    # 0x12 sets the read and notify bits
    assert properties_to_strings(0x12) == ["read", "notify"]
    assert properties_to_strings(0x00) == []


# -- security escalation --------------------------------------------------
async def test_request_escalates_and_retries_on_encryption_error() -> None:
    """An insufficient-encryption error raises security and re-sends once."""
    attempts = 0

    def responder(req: bytes) -> bytes:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return _att_error(req[0], 0x0005, ATT_ERR_INSUFFICIENT_ENCRYPTION)
        return bytes([ATT_READ_RSP]) + b"\x01\x02"

    seen: list[int] = []

    def escalate(err: int) -> bool:
        seen.append(err)
        return True

    peer = FakePeer(responder, escalate_security=escalate)
    assert await peer.client.read(0x0005) == b"\x01\x02"
    assert seen == [ATT_ERR_INSUFFICIENT_ENCRYPTION]
    assert len(peer.sent) == 2  # original request plus one retry


async def test_request_does_not_retry_when_escalation_declines() -> None:
    """If security cannot be raised, the error is surfaced without a retry."""
    peer = FakePeer(
        lambda req: _att_error(req[0], 0x0001, ATT_ERR_INSUFFICIENT_AUTHENTICATION),
        escalate_security=lambda _err: False,
    )
    with pytest.raises(ATTError) as exc_info:
        await peer.client.read(0x0001)
    assert exc_info.value.error_code == ATT_ERR_INSUFFICIENT_AUTHENTICATION
    assert len(peer.sent) == 1  # not retried


async def test_request_escalates_at_most_once() -> None:
    """A persistent security error is surfaced after a single retry."""
    seen: list[int] = []

    def escalate(err: int) -> bool:
        seen.append(err)
        return True

    peer = FakePeer(
        lambda req: _att_error(req[0], 0x0001, ATT_ERR_INSUFFICIENT_ENCRYPTION),
        escalate_security=escalate,
    )
    with pytest.raises(ATTError):
        await peer.client.read(0x0001)
    assert len(seen) == 1  # escalated once
    assert len(peer.sent) == 2  # original request plus one retry


async def test_request_without_escalation_surfaces_security_error() -> None:
    """With no escalation hook, a security error propagates immediately."""
    peer = FakePeer(
        lambda req: _att_error(req[0], 0x0001, ATT_ERR_INSUFFICIENT_ENCRYPTION)
    )
    with pytest.raises(ATTError) as exc_info:
        await peer.client.read(0x0001)
    assert exc_info.value.error_code == ATT_ERR_INSUFFICIENT_ENCRYPTION
    assert len(peer.sent) == 1


# -- MTU ------------------------------------------------------------------
async def test_exchange_mtu_negotiates_minimum() -> None:
    """The negotiated MTU is the minimum of ours and the server's."""
    peer = FakePeer(
        lambda _req: bytes([ATT_EXCHANGE_MTU_RSP]) + (185).to_bytes(2, "little")
    )
    assert await peer.client.exchange_mtu(247) == 185
    assert peer.client.mtu == 185


async def test_exchange_mtu_unsupported_keeps_default() -> None:
    """A peer that does not support MTU exchange falls back to the default."""
    peer = FakePeer(lambda req: _att_error(req[0], 0, ATT_ERR_REQUEST_NOT_SUPPORTED))
    assert await peer.client.exchange_mtu(247) == DEFAULT_MTU
    assert peer.client.mtu == DEFAULT_MTU


async def test_exchange_mtu_propagates_other_errors() -> None:
    """An MTU rejection other than 'not supported' is surfaced, not swallowed."""
    peer = FakePeer(
        lambda req: _att_error(req[0], 0, ATT_ERR_INSUFFICIENT_AUTHENTICATION)
    )
    with pytest.raises(ATTError) as exc_info:
        await peer.client.exchange_mtu(247)
    assert exc_info.value.error_code == ATT_ERR_INSUFFICIENT_AUTHENTICATION


# -- reads ----------------------------------------------------------------
async def test_read_short_value() -> None:
    """A short value is returned without a Read Blob follow-up."""
    peer = FakePeer(lambda _req: bytes([ATT_READ_RSP]) + b"\x01\x02\x03")
    assert await peer.client.read(0x0010) == b"\x01\x02\x03"


async def test_read_long_value_continues_with_blob() -> None:
    """A value is read across multiple full Read Blob chunks until a short one."""
    # With the default MTU (23) a full chunk is mtu - 1 = 22 bytes; a chunk
    # shorter than that ends the read without an extra empty round-trip.
    first = bytes(range(22))  # full -> blob read
    blob_full = bytes(range(100, 122))  # full (22) -> continue
    blob_short = b"\xaa\xbb\xcc\xdd"  # short (4) -> stop

    def responder(req: bytes) -> bytes:
        if req[0] == 0x0A:  # ATT_READ_REQ
            return bytes([ATT_READ_RSP]) + first
        offset = int.from_bytes(req[3:5], "little")
        if offset == 22:
            return bytes([ATT_READ_BLOB_RSP]) + blob_full
        return bytes([ATT_READ_BLOB_RSP]) + blob_short

    peer = FakePeer(responder)
    assert await peer.client.read(0x0010) == first + blob_full + blob_short
    # READ + two READ_BLOB; the short final chunk avoids a trailing empty read.
    assert len(peer.sent) == 3


async def test_read_blob_invalid_offset_stops() -> None:
    """An invalid-offset error during a long read ends the read cleanly."""
    full = bytes(range(22))

    def responder(req: bytes) -> bytes:
        if req[0] == 0x0A:  # ATT_READ_REQ
            return bytes([ATT_READ_RSP]) + full
        return _att_error(req[0], 0x10, ATT_ERR_INVALID_OFFSET)

    peer = FakePeer(responder)
    assert await peer.client.read(0x0010) == full


# -- writes ---------------------------------------------------------------
async def test_write_with_response() -> None:
    """Write Request waits for the Write Response."""
    peer = FakePeer(lambda _req: bytes([ATT_WRITE_RSP]))
    await peer.client.write(0x0011, b"\x01\x00")
    assert peer.sent[0][0] == 0x12  # ATT_WRITE_REQ
    assert peer.sent[0][1:3] == (0x0011).to_bytes(2, "little")
    assert peer.sent[0][3:] == b"\x01\x00"


async def test_write_command_has_no_response() -> None:
    """Write Command is fire-and-forget."""
    peer = FakePeer()
    await peer.client.write_command(0x0011, b"\x09")
    assert peer.sent == [bytes([ATT_WRITE_CMD]) + b"\x11\x00\x09"]


# -- error handling -------------------------------------------------------
async def test_error_response_raises_att_error() -> None:
    """An ATT error response raises ATTError with the details."""
    peer = FakePeer(lambda req: _att_error(req[0], 0x0010, ATT_ERR_READ_NOT_PERMITTED))
    with pytest.raises(ATTError) as exc_info:
        await peer.client.read(0x0010)
    assert exc_info.value.error_code == ATT_ERR_READ_NOT_PERMITTED
    assert exc_info.value.handle == 0x0010


async def test_unexpected_opcode_raises() -> None:
    """A response with the wrong opcode raises a BleakError."""
    peer = FakePeer(lambda _req: bytes([ATT_WRITE_RSP]))  # wrong for a read
    with pytest.raises(BleakError, match="unexpected ATT opcode"):
        await peer.client.read(0x0010)


# -- notifications --------------------------------------------------------
async def test_notification_dispatched_to_handler() -> None:
    """A handle value notification reaches the registered handler."""
    peer = FakePeer()
    received: list[bytearray] = []
    peer.client.set_notify_handler(0x0003, received.append)
    peer.client.data_received(
        bytes([ATT_HANDLE_VALUE_NTF]) + (0x0003).to_bytes(2, "little") + b"\x10\x20"
    )
    assert received == [bytearray(b"\x10\x20")]


async def test_indication_dispatched_and_confirmed() -> None:
    """An indication reaches the handler and is acknowledged."""
    peer = FakePeer()
    received: list[bytearray] = []
    peer.client.set_notify_handler(0x0005, received.append)
    peer.client.data_received(
        bytes([ATT_HANDLE_VALUE_IND]) + (0x0005).to_bytes(2, "little") + b"\x99"
    )
    await asyncio.sleep(0)  # let the fire-and-forget confirm run
    assert received == [bytearray(b"\x99")]
    assert peer.sent == [bytes([ATT_HANDLE_VALUE_CFM])]


async def test_remove_notify_handler() -> None:
    """A removed handler no longer receives notifications."""
    peer = FakePeer()
    received: list[bytearray] = []
    peer.client.set_notify_handler(0x0003, received.append)
    peer.client.remove_notify_handler(0x0003)
    peer.client.data_received(
        bytes([ATT_HANDLE_VALUE_NTF]) + (0x0003).to_bytes(2, "little") + b"\x10"
    )
    assert received == []


async def test_data_received_empty_is_ignored() -> None:
    """An empty inbound PDU is ignored."""
    peer = FakePeer()
    peer.client.data_received(b"")  # must not raise


# -- connection loss ------------------------------------------------------
async def test_connection_lost_fails_pending_and_notifies() -> None:
    """Connection loss fails the in-flight request and calls the callback."""
    disconnects: list[Exception | None] = []
    peer = FakePeer()  # no responder: the request will hang awaiting a reply
    client = ATTClient(peer.send, on_disconnect=disconnects.append)

    task = asyncio.create_task(client.read(0x0010))
    await asyncio.sleep(0)  # let the request go out and await the response
    err = OSError("link lost")
    client.connection_lost(err)
    with pytest.raises(OSError, match="link lost"):
        await task
    assert disconnects == [err]


# -- discovery primitives -------------------------------------------------
async def test_read_by_group_type_parses_entries() -> None:
    """Primary-service discovery parses (handle, end, uuid) entries."""

    def responder(req: bytes) -> bytes:
        start = int.from_bytes(req[1:3], "little")
        if start <= 0x0001:
            return bytes([ATT_READ_BY_GROUP_TYPE_RSP, 6]) + b"\x01\x00\x05\x00\x0d\x18"
        return _att_error(req[0], start, ATT_ERR_ATTRIBUTE_NOT_FOUND)

    peer = FakePeer(responder)
    # End the search at 0x0005 so the loop terminates on the range, not an error.
    results = await peer.client.read_by_group_type(0x0001, 0x0005, b"\x00\x28")
    assert results == [(0x0001, 0x0005, b"\x0d\x18")]
    assert len(peer.sent) == 1


async def test_read_by_type_parses_entries() -> None:
    """Characteristic discovery parses (handle, value) entries."""

    def responder(req: bytes) -> bytes:
        start = int.from_bytes(req[1:3], "little")
        if start <= 0x0002:
            return bytes([ATT_READ_BY_TYPE_RSP, 7]) + b"\x02\x00\x12\x03\x00\x37\x2a"
        return _att_error(req[0], start, ATT_ERR_ATTRIBUTE_NOT_FOUND)

    peer = FakePeer(responder)
    # End at the entry handle so the loop terminates on the range.
    results = await peer.client.read_by_type(0x0001, 0x0002, b"\x03\x28")
    assert results == [(0x0002, b"\x12\x03\x00\x37\x2a")]
    assert len(peer.sent) == 1


async def test_find_information_parses_16_bit() -> None:
    """Descriptor discovery parses 16-bit UUID entries."""

    def responder(req: bytes) -> bytes:
        start = int.from_bytes(req[1:3], "little")
        if start <= 0x0004:
            # format 0x01 (16-bit): handle 0x0004 -> CCCD
            return bytes([ATT_FIND_INFO_RSP, 0x01]) + b"\x04\x00\x02\x29"
        return _att_error(req[0], start, ATT_ERR_ATTRIBUTE_NOT_FOUND)

    peer = FakePeer(responder)
    results = await peer.client.find_information(0x0004, 0x0005)
    assert results == [(0x0004, CCCD_UUID)]


# -- full discovery -------------------------------------------------------
async def test_discover_builds_service_tree() -> None:
    """
    discover() walks services, characteristics and descriptors.

    The service has two characteristics: the first owns a CCCD descriptor (so
    descriptors are fetched) and the second's value handle is the service's last
    handle (so it has no descriptor range to scan).
    """

    def responder(req: bytes) -> bytes:
        opcode = req[0]
        start = int.from_bytes(req[1:3], "little")
        if opcode == ATT_READ_BY_GROUP_TYPE_REQ:
            if start <= 0x0001:
                # service 0x0001-0x0006, uuid 0x180D
                return (
                    bytes([ATT_READ_BY_GROUP_TYPE_RSP, 6]) + b"\x01\x00\x06\x00\x0d\x18"
                )
            return _att_error(opcode, start, ATT_ERR_ATTRIBUTE_NOT_FOUND)
        if opcode == ATT_READ_BY_TYPE_REQ:
            if start <= 0x0002:
                # two characteristics in one response (each 7 bytes):
                # A: decl 0x0002, props 0x12, value 0x0003, uuid 0x2A37
                # B: decl 0x0005, props 0x02, value 0x0006, uuid 0x2A38
                char_a = b"\x02\x00\x12\x03\x00\x37\x2a"
                char_b = b"\x05\x00\x02\x06\x00\x38\x2a"
                return bytes([ATT_READ_BY_TYPE_RSP, 7]) + char_a + char_b
            return _att_error(opcode, start, ATT_ERR_ATTRIBUTE_NOT_FOUND)
        if opcode == ATT_FIND_INFO_REQ:
            # Only characteristic A (value 0x0003, end 0x0004) has a descriptor.
            if start <= 0x0004:
                return bytes([ATT_FIND_INFO_RSP, 0x01]) + b"\x04\x00\x02\x29"
            return _att_error(opcode, start, ATT_ERR_ATTRIBUTE_NOT_FOUND)
        msg = f"unexpected opcode {opcode:#x}"
        raise AssertionError(msg)

    peer = FakePeer(responder)
    services = await peer.client.discover()

    assert len(services) == 1
    svc = services[0]
    assert svc.handle == 0x0001
    assert svc.end_handle == 0x0006
    assert svc.uuid == "0000180d-0000-1000-8000-00805f9b34fb"

    assert len(svc.characteristics) == 2
    char_a, char_b = svc.characteristics

    assert char_a.handle == 0x0002
    assert char_a.value_handle == 0x0003
    assert char_a.uuid == "00002a37-0000-1000-8000-00805f9b34fb"
    assert char_a.properties == 0x12
    assert char_a.end_handle == 0x0004  # one before the next declaration
    assert properties_to_strings(char_a.properties) == ["read", "notify"]
    assert len(char_a.descriptors) == 1
    assert char_a.descriptors[0].handle == 0x0004
    assert char_a.descriptors[0].uuid == CCCD_UUID

    # The last characteristic's value handle is the service end, so no
    # descriptor range remains to scan.
    assert char_b.handle == 0x0005
    assert char_b.value_handle == 0x0006
    assert char_b.uuid == "00002a38-0000-1000-8000-00805f9b34fb"
    assert char_b.end_handle == 0x0006
    assert char_b.descriptors == []


async def test_read_blob_propagates_other_errors() -> None:
    """A non-offset error during a long read propagates."""
    full = bytes(range(22))

    def responder(req: bytes) -> bytes:
        if req[0] == 0x0A:  # ATT_READ_REQ
            return bytes([ATT_READ_RSP]) + full
        return _att_error(req[0], 0x10, ATT_ERR_READ_NOT_PERMITTED)

    peer = FakePeer(responder)
    with pytest.raises(ATTError):
        await peer.client.read(0x0010)


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.read_by_group_type(0x0001, 0xFFFF, b"\x00\x28"),
        lambda c: c.read_by_type(0x0001, 0xFFFF, b"\x03\x28"),
        lambda c: c.find_information(0x0001, 0xFFFF),
    ],
)
async def test_discovery_propagates_non_not_found_errors(
    call: Callable[[ATTClient], Awaitable[object]],
) -> None:
    """A non-ATTR_NOT_FOUND error aborts discovery instead of ending it."""
    peer = FakePeer(lambda req: _att_error(req[0], 0x0001, ATT_ERR_READ_NOT_PERMITTED))
    with pytest.raises(ATTError):
        await call(peer.client)


async def test_read_by_group_type_stops_at_max_handle() -> None:
    """An end handle of 0xFFFF ends the loop without another request."""
    peer = FakePeer(
        lambda _req: (
            bytes([ATT_READ_BY_GROUP_TYPE_RSP, 6]) + b"\x01\x00\xff\xff\x0d\x18"
        )
    )
    results = await peer.client.read_by_group_type(0x0001, 0xFFFF, b"\x00\x28")
    assert results == [(0x0001, 0xFFFF, b"\x0d\x18")]
    assert len(peer.sent) == 1


async def test_read_by_type_stops_at_max_handle() -> None:
    """A handle of 0xFFFF ends the loop without another request."""
    peer = FakePeer(
        lambda _req: bytes([ATT_READ_BY_TYPE_RSP, 7]) + b"\xff\xff\x12\x03\x00\x37\x2a"
    )
    results = await peer.client.read_by_type(0x0001, 0xFFFF, b"\x03\x28")
    assert results[0][0] == 0xFFFF
    assert len(peer.sent) == 1


async def test_find_information_128_bit_and_max_handle() -> None:
    """find_information parses 128-bit UUIDs and stops at the max handle."""
    uuid128 = bytes.fromhex("fb349b5f8000008000100000661a0a05")
    peer = FakePeer(
        lambda _req: bytes([ATT_FIND_INFO_RSP, 0x02]) + b"\xff\xff" + uuid128
    )
    results = await peer.client.find_information(0x0001, 0xFFFF)
    assert results == [(0xFFFF, "050a1a66-0000-1000-8000-00805f9b34fb")]
    assert len(peer.sent) == 1


async def test_no_pending_request_paths() -> None:
    """A stray response and a bare connection loss are handled safely."""
    peer = FakePeer()
    # A response with no in-flight request is ignored.
    peer.client.data_received(bytes([ATT_WRITE_RSP]))
    # Connection loss with no pending request and no callback does not raise.
    peer.client.connection_lost(None)


# -- wire robustness on malformed input -----------------------------------
async def test_uuid_from_bytes_rejects_invalid_length() -> None:
    """A UUID that is neither 2 nor 16 bytes is rejected, not mis-formatted."""
    with pytest.raises(BleakError, match="invalid ATT UUID length"):
        uuid_from_bytes(b"\x01\x02\x03\x04")


async def test_read_by_group_type_rejects_malformed_length() -> None:
    """A zero/short length byte raises instead of crashing range()."""
    peer = FakePeer(lambda _req: bytes([ATT_READ_BY_GROUP_TYPE_RSP, 0]))
    with pytest.raises(BleakError, match="malformed read-by-group-type"):
        await peer.client.read_by_group_type(0x0001, 0xFFFF, b"\x00\x28")


async def test_read_by_type_rejects_malformed_length() -> None:
    """A zero/short length byte raises instead of crashing range()."""
    peer = FakePeer(lambda _req: bytes([ATT_READ_BY_TYPE_RSP, 0]))
    with pytest.raises(BleakError, match="malformed read-by-type"):
        await peer.client.read_by_type(0x0001, 0xFFFF, b"\x03\x28")


async def test_find_information_rejects_invalid_format() -> None:
    """A format byte other than 0x01/0x02 raises rather than mis-parsing."""
    peer = FakePeer(lambda _req: bytes([ATT_FIND_INFO_RSP, 0x03]) + b"\x04\x00")
    with pytest.raises(BleakError, match="invalid find-information format"):
        await peer.client.find_information(0x0004, 0xFFFF)


async def test_indication_confirm_failure_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed indication confirmation is logged, not raised as a task error."""

    async def send(data: bytes) -> None:
        if data == bytes([ATT_HANDLE_VALUE_CFM]):
            msg = "send failed"
            raise OSError(msg)

    client = ATTClient(send)
    client.set_notify_handler(0x0005, lambda _v: None)
    with caplog.at_level("DEBUG", logger="habluetooth.channels.att"):
        client.data_received(
            bytes([ATT_HANDLE_VALUE_IND]) + (0x0005).to_bytes(2, "little") + b"\x99"
        )
        # Let the confirm task run and its done-callback fire.
        for _ in range(3):
            await asyncio.sleep(0)
    assert "Failed to confirm ATT indication" in caplog.text


async def test_request_rejects_truncated_error_response() -> None:
    """A truncated ATT error response raises BleakError, not struct.error."""
    # ATT_ERROR_RSP needs 5 bytes; return only 2.
    peer = FakePeer(lambda _req: bytes([ATT_ERROR_RSP, 0x0A]))
    with pytest.raises(BleakError, match="truncated ATT error response"):
        await peer.client.read(0x0010)


async def test_discover_rejects_malformed_characteristic_declaration() -> None:
    """A characteristic declaration shorter than 5 bytes raises BleakError."""

    def responder(req: bytes) -> bytes:
        opcode = req[0]
        start = int.from_bytes(req[1:3], "little")
        if opcode == ATT_READ_BY_GROUP_TYPE_REQ:
            if start <= 0x0001:
                return (
                    bytes([ATT_READ_BY_GROUP_TYPE_RSP, 6]) + b"\x01\x00\x05\x00\x0d\x18"
                )
            return _att_error(opcode, start, ATT_ERR_ATTRIBUTE_NOT_FOUND)
        if opcode == ATT_READ_BY_TYPE_REQ:
            # length 2 survives read_by_type's guard but leaves an empty value.
            return bytes([ATT_READ_BY_TYPE_RSP, 2]) + b"\x02\x00"
        msg = f"unexpected opcode {opcode:#x}"
        raise AssertionError(msg)

    peer = FakePeer(responder)
    with pytest.raises(BleakError, match="malformed characteristic declaration"):
        await peer.client.discover()


async def test_request_timeout_raises_bleak_error() -> None:
    """A request with no reply times out as a BleakError, not TimeoutError."""
    peer = FakePeer()  # no responder: the future never resolves
    with (
        patch("habluetooth.channels.att.ATT_TIMEOUT", 0.01),
        pytest.raises(BleakError, match="ATT request timed out"),
    ):
        await peer.client.read(0x0010)


async def test_notification_handler_exception_is_isolated(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A raising notification handler is logged, not propagated to data_received."""

    def bad_handler(_value: bytearray) -> None:
        msg = "boom"
        raise ValueError(msg)

    peer = FakePeer()
    peer.client.set_notify_handler(0x0003, bad_handler)
    with caplog.at_level("ERROR", logger="habluetooth.channels.att"):
        # Must not raise out of data_received.
        peer.client.data_received(
            bytes([ATT_HANDLE_VALUE_NTF]) + (0x0003).to_bytes(2, "little") + b"\x10"
        )
    assert "Error in notification handler" in caplog.text


async def test_indication_handler_exception_still_confirms() -> None:
    """A raising indication handler does not block the confirmation."""

    def bad_handler(_value: bytearray) -> None:
        msg = "boom"
        raise ValueError(msg)

    peer = FakePeer()
    peer.client.set_notify_handler(0x0005, bad_handler)
    peer.client.data_received(
        bytes([ATT_HANDLE_VALUE_IND]) + (0x0005).to_bytes(2, "little") + b"\x99"
    )
    await asyncio.sleep(0)
    assert peer.sent == [bytes([ATT_HANDLE_VALUE_CFM])]


@pytest.mark.parametrize(
    ("call", "empty_response"),
    [
        (
            lambda c: c.read_by_group_type(0x0001, 0xFFFF, b"\x00\x28"),
            bytes([ATT_READ_BY_GROUP_TYPE_RSP, 6]),  # valid stride, no entries
        ),
        (
            lambda c: c.read_by_type(0x0001, 0xFFFF, b"\x03\x28"),
            bytes([ATT_READ_BY_TYPE_RSP, 7]),
        ),
        (
            lambda c: c.find_information(0x0001, 0xFFFF),
            bytes([ATT_FIND_INFO_RSP, 0x01]),
        ),
    ],
)
async def test_discovery_rejects_entryless_response(
    call: Callable[[ATTClient], Awaitable[list[object]]],
    empty_response: bytes,
) -> None:
    """An entry-less response is rejected, not treated as a silent partial stop."""
    peer = FakePeer(lambda _req: empty_response)
    with pytest.raises(BleakError, match="truncated entries"):
        await call(peer.client)
    # Exactly one request; the loop neither re-requests nor returns partial data.
    assert len(peer.sent) == 1


@pytest.mark.parametrize(
    ("call", "partial_response"),
    [
        (
            lambda c: c.read_by_group_type(0x0001, 0xFFFF, b"\x00\x28"),
            # stride 6: one full entry plus a 2 byte trailing partial.
            bytes([ATT_READ_BY_GROUP_TYPE_RSP, 6])
            + b"\x01\x00\x05\x00\x0f\x18"
            + b"\x06\x00",
        ),
        (
            lambda c: c.read_by_type(0x0001, 0xFFFF, b"\x03\x28"),
            # stride 7: one full entry plus a 3 byte trailing partial.
            bytes([ATT_READ_BY_TYPE_RSP, 7])
            + b"\x02\x00\x02\x03\x00\x0f\x18"
            + b"\x04\x00\x02",
        ),
        (
            lambda c: c.find_information(0x0001, 0xFFFF),
            # entry size 4: one full entry plus a 2 byte trailing partial.
            bytes([ATT_FIND_INFO_RSP, 0x01]) + b"\x04\x00\x02\x29" + b"\x05\x00",
        ),
    ],
)
async def test_discovery_rejects_trailing_partial_entry(
    call: Callable[[ATTClient], Awaitable[list[object]]],
    partial_response: bytes,
) -> None:
    """A body that is not a whole multiple of the stride is rejected."""
    peer = FakePeer(lambda _req: partial_response)
    with pytest.raises(BleakError, match="truncated entries"):
        await call(peer.client)
    assert len(peer.sent) == 1


async def test_exchange_mtu_rejects_truncated_response() -> None:
    """A truncated MTU response raises rather than negotiating off a stray byte."""
    peer = FakePeer(lambda _req: bytes([ATT_EXCHANGE_MTU_RSP, 0x10]))  # 2 bytes
    with pytest.raises(BleakError, match="truncated ATT MTU response"):
        await peer.client.exchange_mtu(247)


async def test_timeout_poisons_channel() -> None:
    """A request timeout closes the channel so a late response can't desync it."""
    disconnects: list[Exception | None] = []
    peer = FakePeer()  # no responder: the request times out
    client = ATTClient(peer.send, on_disconnect=disconnects.append)
    with (
        patch("habluetooth.channels.att.ATT_TIMEOUT", 0.01),
        pytest.raises(BleakError, match="timed out"),
    ):
        await client.read(0x0010)
    assert len(disconnects) == 1

    # The channel is poisoned: requests and write commands fail fast without
    # sending, so a write command does not hand a PDU to a dead socket.
    sent_before = len(peer.sent)
    with pytest.raises(BleakError, match="ATT channel closed"):
        await client.read(0x0011)
    with pytest.raises(BleakError, match="ATT channel closed"):
        await client.write_command(0x0011, b"\x01")
    assert len(peer.sent) == sent_before

    # A subsequent connection_lost does not notify a second time.
    client.connection_lost(None)
    assert len(disconnects) == 1


async def test_poisoned_channel_drops_inbound_pdus() -> None:
    """A poisoned channel drops late notifications and skips indication confirms."""
    received: list[bytearray] = []
    peer = FakePeer()  # no responder: the request times out
    peer.client.set_notify_handler(0x0005, received.append)
    with (
        patch("habluetooth.channels.att.ATT_TIMEOUT", 0.01),
        pytest.raises(BleakError, match="timed out"),
    ):
        await peer.client.read(0x0010)

    sent_before = len(peer.sent)
    # A late notification is not delivered to the subscriber.
    peer.client.data_received(bytes([ATT_HANDLE_VALUE_NTF, 0x05, 0x00, 0xAB]))
    assert received == []
    # A late indication is dropped without scheduling a confirmation send.
    peer.client.data_received(bytes([ATT_HANDLE_VALUE_IND, 0x05, 0x00, 0xAB]))
    await asyncio.sleep(0)
    assert received == []
    assert len(peer.sent) == sent_before


async def test_write_rejects_oversized_value() -> None:
    """A write larger than mtu - 3 is rejected (no long-write support)."""
    peer = FakePeer()
    with pytest.raises(BleakError, match="value too long"):
        await peer.client.write(0x0011, bytes(21))  # default mtu 23 -> max 20
    assert peer.sent == []


async def test_write_command_rejects_oversized_value() -> None:
    """A write command larger than mtu - 3 is rejected."""
    peer = FakePeer()
    with pytest.raises(BleakError, match="value too long"):
        await peer.client.write_command(0x0011, bytes(21))
    assert peer.sent == []


async def test_read_caps_value_at_offset_range() -> None:
    """An always-full long read is capped instead of overflowing the offset."""
    full = bytes(range(22))  # a full chunk (mtu - 1) every time, never terminating

    def responder(req: bytes) -> bytes:
        if req[0] == 0x0A:  # ATT_READ_REQ
            return bytes([ATT_READ_RSP]) + full
        return bytes([ATT_READ_BLOB_RSP]) + full

    peer = FakePeer(responder)
    with pytest.raises(BleakError, match="exceeds the 16-bit"):
        await peer.client.read(0x0010)


async def test_truncated_notification_is_dropped() -> None:
    """A notification PDU too short to carry a handle is dropped, not fabricated."""
    peer = FakePeer()
    received: list[bytearray] = []
    peer.client.set_notify_handler(0x0005, received.append)
    # opcode + a single handle byte: too short to carry a 2-byte handle.
    peer.client.data_received(bytes([ATT_HANDLE_VALUE_NTF, 0x05]))
    assert received == []


async def test_truncated_indication_is_dropped() -> None:
    """A truncated indication is dropped and not confirmed."""
    peer = FakePeer()
    peer.client.data_received(bytes([ATT_HANDLE_VALUE_IND, 0x05]))
    await asyncio.sleep(0)
    assert peer.sent == []  # no CFM sent for a malformed PDU
