"""
Kernel/L2CAP GATT client backend (bleak ``BaseBleakClient``).

``HaMgmtClient`` is a bleak-compatible client that talks to a peripheral over a
raw L2CAP ATT channel instead of going through bluetoothd/DBus. It wires the
:class:`~habluetooth.channels.l2cap.L2CAPSocket` transport to the
:class:`~habluetooth.channels.att.ATTClient` codec, runs MTU exchange and GATT
discovery on connect, and translates the discovered tree into bleak's unified
GATT model so existing integrations can use it unchanged.

``create_local_scanner`` routes connections here for local adapters, but Home
Assistant does not call that factory yet, so nothing reaches this backend in
production today.

Pairing is driven over the management socket (Just Works). ``connect(pair=True)``
and ``pair()`` bond with the peer and capture the long-term key so a later
reconnect can restore it; on a reconnect for an already-bonded peer the socket
requests encryption up front so the kernel re-encrypts the link, and any ATT
operation the peer rejects for insufficient security escalates on demand.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast

from bleak import BleakError
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.client import BaseBleakClient
from bleak.backends.descriptor import BleakGATTDescriptor
from bleak.backends.service import BleakGATTService, BleakGATTServiceCollection

from .channels.att import (
    ATT_ERR_INSUFFICIENT_ENCRYPTION,
    CCCD_UUID,
    DEFAULT_MTU,
    PREFERRED_MTU,
    ATTClient,
    properties_to_strings,
)
from .channels.bluez import (
    AuthenticationFailed,
    NewLongTermKey,
    UserConfirmationRequest,
)
from .channels.l2cap import (
    BT_SECURITY_HIGH,
    BT_SECURITY_LOW,
    BT_SECURITY_MEDIUM,
    L2CAPSocket,
)
from .const import BDADDR_LE_PUBLIC

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager

    from bleak.args import SizedBuffer
    from bleak.assigned_numbers import CharacteristicPropertyName
    from bleak.backends.device import BLEDevice

    from .channels.att import GattService
    from .channels.bluez import (
        LongTermKey,
        MGMTBluetoothCtl,
        UserPasskeyRequest,
    )

    PairingEvent = (
        NewLongTermKey
        | AuthenticationFailed
        | UserConfirmationRequest
        | UserPasskeyRequest
    )


class SupportsConnecting(Protocol):
    """The slice of a scanner the client needs: a scan-pause context manager."""

    def connecting(self) -> AbstractContextManager[None]:
        """Pause scanning for the duration of a connection attempt."""


_LOGGER = logging.getLogger(__name__)

# Fallback connect timeout; the bleak wrapper always supplies ``timeout``.
DEFAULT_TIMEOUT = 10.0

# Client Characteristic Configuration Descriptor payloads (Core Vol 3 Part G).
_CCCD_NOTIFY = b"\x01\x00"
_CCCD_INDICATE = b"\x02\x00"
_CCCD_OFF = b"\x00\x00"


@dataclass(slots=True)
class MgmtClientData:
    """Per-connection wiring the mgmt scanner hands to each client instance."""

    adapter_address: str  # local adapter BD_ADDR the L2CAP socket binds/sends from
    scanner: SupportsConnecting  # provides connecting() to pause scanning
    # Optional slot bookkeeping: the mgmt scanner tracks live connections itself
    # (BleakSlotManager is DBus-path based and cannot see L2CAP connections), so
    # the client reports connect/disconnect by peer address here.
    register_connection: Callable[[str], None] | None = None
    unregister_connection: Callable[[str], None] | None = None
    # Pairing: the scanner supplies the mgmt controller, the adapter index, and a
    # per-adapter long-term-key store (bonds must outlive a per-connection client
    # instance so reconnects can restore them).
    adapter_idx: int | None = None
    mgmt: MGMTBluetoothCtl | None = None
    # get_long_term_keys returns every bonded key for the adapter (LOAD_LONG_TERM_
    # KEYS replaces the controller's whole list, so a restore must send them all);
    # add_long_term_key stores one captured key; forget_long_term_keys drops all
    # keys for a peer on unpair.
    get_long_term_keys: Callable[[], list[LongTermKey]] | None = None
    add_long_term_key: Callable[[LongTermKey], None] | None = None
    forget_long_term_keys: Callable[[str], None] | None = None


class HaMgmtClient(BaseBleakClient):
    """A bleak client that drives GATT over a raw L2CAP ATT channel."""

    def __init__(
        self,
        address_or_ble_device: BLEDevice | str,
        *args: Any,
        client_data: MgmtClientData,
        **kwargs: Any,
    ) -> None:
        """Bind the client to its peer address and adapter wiring."""
        kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
        super().__init__(address_or_ble_device, *args, **kwargs)
        self._adapter_address = client_data.adapter_address
        self._scanner = client_data.scanner
        self._register_connection = client_data.register_connection
        self._unregister_connection = client_data.unregister_connection
        self._adapter_idx = client_data.adapter_idx
        self._mgmt = client_data.mgmt
        self._get_long_term_keys = client_data.get_long_term_keys
        self._add_long_term_key = client_data.add_long_term_key
        self._forget_long_term_keys = client_data.forget_long_term_keys
        details = getattr(address_or_ble_device, "details", None) or {}
        if "address_type" in details:
            self._address_type: int = details["address_type"]
        else:
            # Adverts always carry the peer address type, so the scanner should
            # populate it; fall back to public but log so a missing field is not
            # only visible as an opaque L2CAP connect timeout later.
            self._address_type = BDADDR_LE_PUBLIC
            _LOGGER.debug(
                "%s: no address_type in details; assuming LE public", self.address
            )
        self._att: ATTClient | None = None
        self._sock: L2CAPSocket | None = None
        self._connected = False
        # Strong references to in-flight pairing reply tasks (e.g. an auto-sent
        # confirmation) so they are not garbage-collected before they send.
        self._pairing_tasks: set[asyncio.Task[Any]] = set()

    # -- properties --------------------------------------------------------
    @property
    def is_connected(self) -> bool:
        """Whether the L2CAP/ATT channel is currently up."""
        return self._connected

    @property
    def mtu_size(self) -> int:
        """The negotiated ATT MTU, or the default until it is exchanged."""
        return self._att.mtu if self._att is not None else DEFAULT_MTU

    # -- connection --------------------------------------------------------
    async def connect(self, pair: bool, **kwargs: Any) -> None:
        """Open the L2CAP channel, exchange MTU, and discover services."""
        if self._connected:
            msg = "already connected"
            raise BleakError(msg)
        await self._restore_bond()
        # If we already hold a key for this peer, request encryption up front so
        # the reconnect encrypts like bluetoothd rather than running MTU exchange
        # and discovery in the clear until the first gated op is rejected. The
        # on-demand escalation in the codec still backstops everything else.
        security_level = (
            BT_SECURITY_MEDIUM if self._has_stored_bond() else BT_SECURITY_LOW
        )
        att = ATTClient(
            send=self._send_pdu,
            on_disconnect=self._handle_disconnect,
            escalate_security=self._escalate_security,
        )
        self._att = att
        try:
            with self._scanner.connecting():
                self._sock = await L2CAPSocket.create_connection(
                    source=self._adapter_address,
                    address=self.address,
                    address_type=self._address_type,
                    on_data=att.data_received,
                    on_close=att.connection_lost,
                    timeout=self._timeout,
                    security_level=security_level,
                )
                if pair:
                    # Honor bleak's connect(pair=True): bond as part of
                    # connecting so callers that pair-then-use get a real bond
                    # (and an encrypted link) before MTU exchange and discovery.
                    await self._pair_if_possible()
                await att.exchange_mtu(PREFERRED_MTU)
                services = await att.discover()
        except BaseException:
            self._handle_disconnect(None)
            raise
        self.services = self._build_services(services)
        self._connected = True
        if self._register_connection is not None:
            # Slot bookkeeping is best-effort; a failure must not undo a
            # connection that is already established.
            try:
                self._register_connection(self.address)
            except Exception:
                _LOGGER.exception(
                    "%s: connection slot register callback failed", self.address
                )

    async def disconnect(self) -> None:
        """
        Close the channel; idempotent.

        Like the BlueZ backend this replaces (and unlike CoreBluetooth/WinRT),
        a deliberate disconnect also fires the disconnected callback.
        """
        self._handle_disconnect(None)

    async def _send_pdu(self, data: bytes) -> None:
        """Write one ATT PDU through the transport (bound into the codec)."""
        # _att is created before the socket, but its send is only driven after
        # create_connection has returned and assigned _sock.
        if self._sock is None:  # pragma: no cover - send only runs once connected
            msg = "transport not connected"
            raise BleakError(msg)
        await self._sock.send(data)

    def _escalate_security(self, att_error: int) -> bool:
        """
        Raise the link security to satisfy an ATT auth/encryption rejection.

        Mirrors bluetoothd: insufficient encryption (0x0F) requests MEDIUM,
        insufficient authentication (0x05) steps up one level. The kernel only
        exposes LOW/MEDIUM/HIGH, so nothing above HIGH can be tried. Returns
        whether the level was raised, so the codec knows to re-issue once.
        """
        if self._sock is None:  # pragma: no cover - only called once connected
            return False
        if att_error == ATT_ERR_INSUFFICIENT_ENCRYPTION:
            target = BT_SECURITY_MEDIUM
        else:  # ATT_ERR_INSUFFICIENT_AUTHENTICATION
            target = self._sock.security_level + 1
        if target > BT_SECURITY_HIGH:
            return False
        raised = self._sock.set_security_level(target)
        if raised:
            _LOGGER.debug(
                "%s: raised link security to level %d after ATT error 0x%02x",
                self.address,
                target,
                att_error,
            )
        return raised

    def _handle_disconnect(self, exc: Exception | None) -> None:
        """Tear the channel down once and notify on an unexpected drop."""
        if exc is not None:
            # The codec only passes a cause on an unexpected drop; a deliberate
            # disconnect() passes None and stays silent. bleak's callback takes
            # no args, so this log is the only place the reason can surface.
            _LOGGER.debug("%s: L2CAP/ATT channel lost: %s", self.address, exc)
        was_connected = self._connected
        self._connected = False
        self._att = None
        if self._sock is not None:
            # Idempotent: a transport-level drop already closed it; a request
            # timeout poisons the codec without closing, so close it here.
            self._sock.close()
            self._sock = None
        if was_connected:
            if self._unregister_connection is not None:
                # Best-effort: a bookkeeping failure must not stop the
                # disconnected callback from firing.
                try:
                    self._unregister_connection(self.address)
                except Exception:
                    _LOGGER.exception(
                        "%s: connection slot unregister callback failed",
                        self.address,
                    )
            if self._disconnected_callback is not None:
                self._disconnected_callback()

    # -- GATT model --------------------------------------------------------
    def _build_services(
        self, services: list[GattService]
    ) -> BleakGATTServiceCollection:
        """Translate the discovered tree into bleak's GATT collection."""
        collection = BleakGATTServiceCollection()
        for svc in services:
            bleak_service = BleakGATTService(svc, svc.handle, svc.uuid)
            collection.add_service(bleak_service)
            for char in svc.characteristics:
                bleak_char = BleakGATTCharacteristic(
                    char,
                    # Reads/writes target the value handle, so that is the
                    # handle bleak resolves operations against.
                    char.value_handle,
                    char.uuid,
                    cast(
                        "list[CharacteristicPropertyName]",
                        properties_to_strings(char.properties),
                    ),
                    self._max_write_without_response_size,
                    bleak_service,
                )
                collection.add_characteristic(bleak_char)
                for desc in char.descriptors:
                    collection.add_descriptor(
                        BleakGATTDescriptor(desc, desc.handle, desc.uuid, bleak_char)
                    )
        return collection

    def _max_write_without_response_size(self) -> int:
        """Largest write-without-response payload for the current MTU."""
        return self.mtu_size - 3

    # -- GATT operations ---------------------------------------------------
    async def read_gatt_char(
        self,
        characteristic: BleakGATTCharacteristic,
        *,
        use_cached: bool = False,
        **kwargs: Any,
    ) -> bytearray:
        """Read a characteristic value by its value handle."""
        return bytearray(await self._codec().read(characteristic.handle))

    async def read_gatt_descriptor(
        self,
        descriptor: BleakGATTDescriptor,
        *,
        use_cached: bool = False,
        **kwargs: Any,
    ) -> bytearray:
        """Read a descriptor value by its handle."""
        return bytearray(await self._codec().read(descriptor.handle))

    async def write_gatt_char(
        self,
        characteristic: BleakGATTCharacteristic,
        data: SizedBuffer,
        response: bool,
    ) -> None:
        """Write a characteristic value, with or without a response."""
        codec = self._codec()
        if response:
            await codec.write(characteristic.handle, bytes(data))
        else:
            await codec.write_command(characteristic.handle, bytes(data))

    async def write_gatt_descriptor(
        self, descriptor: BleakGATTDescriptor, data: SizedBuffer
    ) -> None:
        """Write a descriptor value with a response."""
        await self._codec().write(descriptor.handle, bytes(data))

    async def start_notify(
        self,
        characteristic: BleakGATTCharacteristic,
        callback: Callable[[bytearray], None],
        **kwargs: Any,
    ) -> None:
        """Enable notifications/indications by writing the CCCD and routing them."""
        codec = self._codec()
        if "notify" in characteristic.properties:
            cccd_value = _CCCD_NOTIFY
        elif "indicate" in characteristic.properties:
            cccd_value = _CCCD_INDICATE
        else:
            msg = "characteristic does not support notify or indicate"
            raise BleakError(msg)
        cccd = characteristic.get_descriptor(CCCD_UUID)
        if cccd is None:
            msg = "characteristic has no client configuration descriptor"
            raise BleakError(msg)
        # Register before enabling so a notification racing in right after the
        # CCCD write is not lost; unwind if the write fails so no handler is left
        # registered for notifications that were never enabled.
        codec.set_notify_handler(characteristic.handle, callback)
        try:
            await codec.write(cccd.handle, cccd_value)
        except BaseException:
            codec.remove_notify_handler(characteristic.handle)
            raise

    async def stop_notify(self, characteristic: BleakGATTCharacteristic) -> None:
        """Disable notifications by clearing the CCCD and dropping the handler."""
        codec = self._codec()
        cccd = characteristic.get_descriptor(CCCD_UUID)
        if cccd is not None:
            await codec.write(cccd.handle, _CCCD_OFF)
        codec.remove_notify_handler(characteristic.handle)

    # -- pairing -----------------------------------------------------------
    async def _restore_bond(self) -> None:
        """Reload all bonded keys so the kernel re-encrypts known peers."""
        if (
            self._mgmt is None
            or self._adapter_idx is None
            or self._get_long_term_keys is None
        ):
            return
        # LOAD_LONG_TERM_KEYS replaces the controller's whole list, so send every
        # stored key; the kernel ignores keys for peers that are not connecting,
        # and we must not wipe other peers' bonds on this adapter.
        keys = self._get_long_term_keys()
        if keys and not await self._mgmt.load_long_term_keys(self._adapter_idx, keys):
            _LOGGER.warning(
                "%s: failed to restore %d bonded key(s); the link may not encrypt",
                self.address,
                len(keys),
            )

    def _has_stored_bond(self) -> bool:
        """Whether a long-term key is stored for this peer."""
        if self._get_long_term_keys is None:
            return False
        address = self.address.upper()
        return any(key.address.upper() == address for key in self._get_long_term_keys())

    def _require_mgmt(self) -> tuple[MGMTBluetoothCtl, int]:
        """Return the mgmt controller and adapter index, or raise if unavailable."""
        if self._mgmt is None or self._adapter_idx is None:
            msg = "pairing requires the management socket"
            raise BleakError(msg)
        return self._mgmt, self._adapter_idx

    async def _pair_if_possible(self) -> None:
        """Bond as part of connect when needed and mgmt is wired."""
        if self._has_stored_bond():
            # Already bonded: the proactive MEDIUM handles re-encryption, and
            # re-pairing a bonded peer is rejected (ALREADY_PAIRED), which would
            # otherwise fail the connect.
            return
        if self._mgmt is None or self._adapter_idx is None:
            _LOGGER.debug(
                "%s: connect(pair=True) but the management socket is not wired; "
                "skipping bond",
                self.address,
            )
            return
        await self.pair()

    async def pair(self, *args: Any, **kwargs: Any) -> None:
        """
        Bond with the peer over the management socket (Just Works).

        Auto-accepts a Just Works confirmation (the only interaction a
        NoInputNoOutput controller can satisfy) so such a peer does not stall the
        bond, but rejects a real numeric comparison it cannot verify; and fails
        fast on an authentication failure instead of waiting out the mgmt command
        timeout. The captured key is stored for reconnects.
        """
        mgmt, adapter_idx = self._require_mgmt()
        address = self.address
        loop = asyncio.get_running_loop()
        auth_failed: asyncio.Future[AuthenticationFailed] = loop.create_future()

        def _capture(event: PairingEvent) -> None:
            if isinstance(event, NewLongTermKey):
                self._store_captured_key(event)
            elif isinstance(event, UserConfirmationRequest):
                # confirm_hint != 0 means "just confirm, no number to compare"
                # (the Just Works case a NoInputNoOutput controller can satisfy),
                # so accept it; confirm_hint == 0 carries a real numeric value we
                # cannot verify with no IO, so reject rather than blindly confirm
                # and defeat the comparison's MITM protection. Runs in the read
                # loop, so the async reply is scheduled with a strong reference.
                accept = bool(event.confirm_hint)
                task = loop.create_task(
                    mgmt.user_confirmation_reply(
                        adapter_idx, address, self._address_type, accept=accept
                    )
                )
                self._pairing_tasks.add(task)
                task.add_done_callback(self._on_pairing_task_done)
            elif isinstance(event, AuthenticationFailed) and not auth_failed.done():
                auth_failed.set_result(event)

        unregister = mgmt.register_pairing_handler(adapter_idx, address, _capture)
        pair_task = loop.create_task(
            mgmt.pair_device(adapter_idx, address, self._address_type)
        )
        try:
            await asyncio.wait(
                (pair_task, auth_failed), return_when=asyncio.FIRST_COMPLETED
            )
            if auth_failed.done() and not pair_task.done():
                # Surface the failure now rather than waiting out pair_device's
                # mgmt timeout.
                event = auth_failed.result()
                msg = f"{address}: pairing failed (auth status 0x{event.status:02x})"
                raise BleakError(msg)
            if not await pair_task:
                msg = f"{address}: pairing failed"
                raise BleakError(msg)
        finally:
            unregister()
            if not pair_task.done():
                pair_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pair_task

    def _store_captured_key(self, event: NewLongTermKey) -> None:
        """Persist a captured long-term key, honoring the kernel's store hint."""
        if not event.store_hint:
            # The kernel is signalling a key it does not want persisted.
            return
        if self._add_long_term_key is None:
            _LOGGER.warning(
                "%s: captured a long-term key but no key store is wired", self.address
            )
            return
        self._add_long_term_key(event.key)

    def _on_pairing_task_done(self, task: asyncio.Task[Any]) -> None:
        """Drop a finished pairing reply task and log any escaped failure."""
        self._pairing_tasks.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:
            _LOGGER.debug("%s: pairing reply task failed: %s", self.address, exc)

    async def unpair(self) -> None:
        """Remove the bond over mgmt and forget the stored key."""
        mgmt, adapter_idx = self._require_mgmt()
        if not await mgmt.unpair_device(adapter_idx, self.address, self._address_type):
            # Don't drop the local key if the kernel bond is still there, or the
            # store would silently desync from the controller.
            msg = f"{self.address}: unpair failed"
            raise BleakError(msg)
        if self._forget_long_term_keys is not None:
            self._forget_long_term_keys(self.address)

    def _codec(self) -> ATTClient:
        """Return the live ATT codec or raise if the channel is down."""
        if self._att is None or not self._connected:
            msg = "not connected"
            raise BleakError(msg)
        return self._att
