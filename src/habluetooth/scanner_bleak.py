"""A local bleak scanner."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import platform
from functools import lru_cache
from typing import TYPE_CHECKING, Any, no_type_check

import async_interrupt
import bleak
from bleak import BleakError
from bleak.assigned_numbers import AdvertisementDataType
from bleak_retry_connector import Allocations, restore_discoveries
from bleak_retry_connector.bluez import stop_discovery
from bluetooth_adapters import DEFAULT_ADDRESS
from bluetooth_data_tools import monotonic_time_coarse

from .base_scanner import BaseHaScanner
from .const import (
    CALLBACK_TYPE,
    SCANNER_WATCHDOG_INTERVAL,
    SCANNER_WATCHDOG_TIMEOUT,
    SOURCE_LOCAL,
    START_TIMEOUT,
    STOP_TIMEOUT,
)
from .models import BluetoothScanningMode, BluetoothServiceInfoBleak
from .util import async_reset_adapter, is_docker_env

if TYPE_CHECKING:
    from collections.abc import Coroutine, Iterable

    from bleak.backends.device import BLEDevice
    from bleak.backends.scanner import AdvertisementData, AdvertisementDataCallback

int_ = int

SYSTEM = platform.system()
IS_LINUX = SYSTEM == "Linux"
IS_MACOS = SYSTEM == "Darwin"

if IS_LINUX:
    from bleak.args.bluez import BlueZScannerArgs, OrPattern
    from bleak.backends.bluezdbus.advertisement_monitor import (
        AdvertisementMonitor,
    )
    from dbus_fast import InvalidMessageError
    from dbus_fast.service import method

    # or_patterns is a workaround for the fact that passive scanning
    # needs at least one matcher to be set. The below matcher
    # will match all devices.
    PASSIVE_SCANNER_ARGS = BlueZScannerArgs(
        or_patterns=[
            OrPattern(0, AdvertisementDataType.FLAGS, b"\x02"),
            OrPattern(0, AdvertisementDataType.FLAGS, b"\x06"),
            OrPattern(0, AdvertisementDataType.FLAGS, b"\x1a"),
        ]
    )

    class HaAdvertisementMonitor(AdvertisementMonitor):
        """Implementation of the org.bluez.AdvertisementMonitor1 D-Bus interface."""

        # Method names are dictated by the BlueZ AdvertisementMonitor1
        # D-Bus interface; ``dbus_fast`` matches the Python attribute
        # name against the interface, so the CamelCase form is required.
        @method()
        @no_type_check
        def DeviceFound(self, device: o):  # noqa: F821, N802
            """Device found."""

        @method()
        @no_type_check
        def DeviceLost(self, device: o):  # noqa: F821, N802
            """Device lost."""

    AdvertisementMonitor.DeviceFound = HaAdvertisementMonitor.DeviceFound
    AdvertisementMonitor.DeviceLost = HaAdvertisementMonitor.DeviceLost
else:

    class InvalidMessageError(Exception):  # type: ignore[no-redef]
        """Invalid message error."""


OriginalBleakScanner = bleak.BleakScanner

# Pin the logger name to the pre-split module so log levels, filters, and
# log scrapers configured for "habluetooth.scanner" keep working unchanged
# after the move to scanner_bleak.
_LOGGER = logging.getLogger("habluetooth.scanner")

IN_PROGRESS_ERROR = "org.bluez.Error.InProgress"

# If the adapter is in a stuck state the following errors are raised:
NEED_RESET_ERRORS = [
    "org.bluez.Error.Failed",
    IN_PROGRESS_ERROR,
    "org.bluez.Error.NotReady",
    "not found",
]

# When the adapter is still initializing, the scanner will raise an exception
# with org.freedesktop.DBus.Error.UnknownObject
WAIT_FOR_ADAPTER_TO_INIT_ERRORS = ["org.freedesktop.DBus.Error.UnknownObject"]
ADAPTER_INIT_TIME = 1.5

START_ATTEMPTS = 4

SCANNING_MODE_TO_BLEAK = {
    BluetoothScanningMode.ACTIVE: "active",
    BluetoothScanningMode.PASSIVE: "passive",
}


def _resolve_radio_mode(mode: BluetoothScanningMode) -> BluetoothScanningMode:
    """
    Resolve AUTO to the underlying mode the radio actually runs in.

    AUTO is a habluetooth scheduling concept, not a radio state. The
    backend always runs in either passive or active. current_mode is
    supposed to reflect that real state so diagnostics and the manager
    callbacks line up with what remote scanners (e.g. ESPHome) already
    report; otherwise local adapters look stuck on "auto" forever.

    Single source of truth for the AUTO -> radio mapping; both
    create_bleak_scanner and the active-window toggle defer here so a
    future platform change (or a new platform) only needs to update
    this one function.
    """
    if mode is BluetoothScanningMode.AUTO:
        return (
            BluetoothScanningMode.ACTIVE if IS_MACOS else BluetoothScanningMode.PASSIVE
        )
    return mode


# The minimum number of seconds to know
# the adapter has not had advertisements
# and we already tried to restart the scanner
# without success when the first time the watch
# dog hit the failure path.
SCANNER_WATCHDOG_MULTIPLE = (
    SCANNER_WATCHDOG_TIMEOUT + SCANNER_WATCHDOG_INTERVAL.total_seconds()
)


class _AbortStartError(Exception):
    """Error to indicate that the start should be aborted."""


class ScannerStartError(Exception):
    """Error to indicate that the scanner failed to start."""


def create_bleak_scanner(
    detection_callback: AdvertisementDataCallback | None,
    scanning_mode: BluetoothScanningMode,
    adapter: str | None,
) -> bleak.BleakScanner:
    """Create a Bleak scanner."""
    # Resolve AUTO before doing anything else so the rest of this
    # function only ever sees ACTIVE or PASSIVE; CoreBluetooth has no
    # passive mode so AUTO collapses to ACTIVE on macOS (the radio
    # just stays in active and async_request_active_window is a no-op
    # there), and Linux/other platforms start AUTO in passive with
    # the scheduler flipping to active on demand.
    scanning_mode = _resolve_radio_mode(scanning_mode)
    scanner_kwargs: dict[str, Any] = {
        "scanning_mode": SCANNING_MODE_TO_BLEAK[scanning_mode],
    }
    if detection_callback:
        scanner_kwargs["detection_callback"] = detection_callback
    if IS_LINUX:
        # Only Linux supports multiple adapters
        bluez_args: BlueZScannerArgs = {}
        # bleak's passive scanner needs at least one or_pattern matcher
        # or it won't start. AUTO has been resolved to PASSIVE above on
        # Linux (the scheduler restarts with scan_mode_override=ACTIVE
        # to flip to active on demand, which lands here as ACTIVE and
        # skips this branch).
        if scanning_mode is BluetoothScanningMode.PASSIVE:
            bluez_args = dict(PASSIVE_SCANNER_ARGS)
        if adapter:
            # bleak 3.0 deprecated the top-level ``adapter`` kwarg in favor of
            # the ``bluez`` kwarg; this form is supported across bleak 1.x-3.x.
            bluez_args["adapter"] = adapter
        if bluez_args:
            scanner_kwargs["bluez"] = bluez_args
    elif IS_MACOS:
        # We want mac address on macOS
        scanner_kwargs["cb"] = {"use_bdaddr": True}
    _LOGGER.debug("Initializing bluetooth scanner with %s", scanner_kwargs)

    try:
        return OriginalBleakScanner(**scanner_kwargs)
    except (FileNotFoundError, BleakError) as ex:
        msg = f"Failed to initialize Bluetooth: {ex}"
        raise RuntimeError(msg) from ex


def _error_indicates_reset_needed(error_str: str) -> bool:
    """Return if the error indicates a reset is needed."""
    return any(
        needs_reset_error in error_str for needs_reset_error in NEED_RESET_ERRORS
    )


def _error_indicates_wait_for_adapter_to_init(error_str: str) -> bool:
    """Return if the error indicates the adapter is still initializing."""
    return any(
        wait_error in error_str for wait_error in WAIT_FOR_ADAPTER_TO_INIT_ERRORS
    )


@lru_cache(maxsize=512)
def bytes_mac_to_str(mac: bytes) -> str:
    """Convert a MAC address in bytes to a string in big-endian (MSB-first) order."""
    return ":".join(f"{b:02X}" for b in reversed(mac))


@lru_cache(maxsize=512)
def make_bluez_details(address: str, adapter: str) -> dict[str, Any]:
    """Make the details for a bluez advertisement."""
    base_path = f"/org/bluez/{adapter}"
    return {
        "path": f"{base_path}/dev_{address.replace(':', '_')}",
        "props": {
            "Adapter": base_path,
        },
    }


class HaScanner(BaseHaScanner):
    """
    Operate and automatically recover a BleakScanner.

    Multiple BleakScanner can be used at the same time
    if there are multiple adapters. This is only useful
    if the adapters are not located physically next to each other.

    Example use cases are usbip, a long extension cable, usb to bluetooth
    over ethernet, usb over ethernet, etc.
    """

    __slots__ = (
        "_active_window_end",
        "_active_window_handle",
        "_background_tasks",
        "_scan_mode_override",
        "_start_future",
        "_start_stop_lock",
        "mac_address",
        "scanner",
    )

    def __init__(
        self,
        mode: BluetoothScanningMode,
        adapter: str,
        address: str,
    ) -> None:
        """Init bluetooth discovery."""
        self.mac_address = address
        source = address if address != DEFAULT_ADDRESS else adapter or SOURCE_LOCAL
        super().__init__(source, adapter, requested_mode=mode)
        self.connectable = True
        self._start_stop_lock = asyncio.Lock()
        self.scanning = False
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self.scanner: bleak.BleakScanner | None = None
        self._start_future: asyncio.Future[None] | None = None
        # Set while an on-demand active window (auto-mode) is in flight.
        # When set, `_async_start_attempt` uses this mode instead of
        # `requested_mode`. `requested_mode` itself stays at AUTO so external
        # listeners still see the integration's intent.
        self._scan_mode_override: BluetoothScanningMode | None = None
        self._active_window_handle: asyncio.TimerHandle | None = None
        self._active_window_end: float = 0.0

    def _create_background_task(self, coro: Coroutine[Any, Any, None]) -> None:
        """Create a background task and add it to the background tasks set."""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    @property
    def discovered_devices(self) -> list[BLEDevice]:
        """Return a list of discovered devices."""
        if not self.scanner:
            return []
        return self.scanner.discovered_devices

    @property
    def discovered_devices_and_advertisement_data(
        self,
    ) -> dict[str, tuple[BLEDevice, AdvertisementData]]:
        """Return a list of discovered devices and advertisement data."""
        if not self.scanner:
            return {}
        return self.scanner.discovered_devices_and_advertisement_data

    @property
    def discovered_addresses(self) -> Iterable[str]:
        """Return an iterable of discovered devices."""
        return self.discovered_devices_and_advertisement_data

    def get_discovered_device_advertisement_data(
        self, address: str
    ) -> tuple[BLEDevice, AdvertisementData] | None:
        """Return the advertisement data for a discovered device."""
        return self.discovered_devices_and_advertisement_data.get(address)

    def get_allocations(self) -> Allocations | None:
        """Get current connection slot allocations from BleakSlotManager."""
        if self._manager and self._manager.slot_manager:
            return self._manager.slot_manager.get_allocations(self.adapter)
        return None

    def async_setup(self) -> CALLBACK_TYPE:
        """Set up the scanner."""
        super().async_setup()
        return self._unsetup

    async def async_diagnostics(self) -> dict[str, Any]:
        """Return diagnostic information about the scanner."""
        base_diag = await super().async_diagnostics()
        return base_diag | {"adapter": self.adapter}

    def _async_on_raw_bluez_advertisement(
        self,
        address: bytes,
        address_type: int_,
        rssi: int_,
        flags: int_,
        data: bytes,
    ) -> None:
        """Handle raw advertisement data."""
        address_str = bytes_mac_to_str(address)
        self._async_on_raw_advertisement(
            address_str,
            rssi,
            data,
            make_bluez_details(address_str, self.adapter),
            monotonic_time_coarse(),
        )

    def _async_detection_callback(
        self,
        device: BLEDevice,
        advertisement_data: AdvertisementData,
    ) -> None:
        """
        Call the callback when an advertisement is received.

        Currently this is used to feed the callbacks into the
        central manager.
        """
        callback_time = monotonic_time_coarse()
        address = device.address
        local_name = advertisement_data.local_name
        manufacturer_data = advertisement_data.manufacturer_data
        service_data = advertisement_data.service_data
        service_uuids = advertisement_data.service_uuids
        if local_name or manufacturer_data or service_data or service_uuids:
            # Don't count empty advertisements
            # as the adapter is in a failure
            # state if all the data is empty.
            self._last_detection = callback_time
        name = local_name or device.name or address
        if name is not None and type(name) is not str:
            name = str(name)
        tx_power = advertisement_data.tx_power
        if tx_power is not None and type(tx_power) is not int:
            tx_power = int(tx_power)
        service_info = BluetoothServiceInfoBleak.__new__(BluetoothServiceInfoBleak)
        service_info.name = name
        service_info.address = address
        service_info.rssi = advertisement_data.rssi
        service_info.manufacturer_data = manufacturer_data
        service_info.service_data = service_data
        service_info.service_uuids = service_uuids
        service_info.source = self.source
        service_info.device = device
        service_info._advertisement = advertisement_data
        service_info.connectable = True
        service_info.time = callback_time
        service_info.tx_power = tx_power
        service_info.raw = None  # not available in bleak.
        self._manager._scanner_adv_received(service_info)

    async def async_start(self) -> None:
        """Start bluetooth scanner."""
        async with self._start_stop_lock:
            await self._async_start()

    async def _async_start(self) -> None:
        """Start bluetooth scanner under the lock."""
        for attempt in range(1, START_ATTEMPTS + 1):
            if await self._async_start_attempt(attempt):
                # Everything is fine, break out of the loop
                break
        await self._async_on_successful_start()

    async def _async_on_successful_start(self) -> None:
        """Run when the scanner has successfully started."""
        self.scanning = True
        self._async_setup_scanner_watchdog()
        await restore_discoveries(self.scanner, self.adapter)

    def _effective_mode(self) -> BluetoothScanningMode | None:
        """
        Mode the scanner should actually start in.

        Override beats requested_mode so the scheduler can flip AUTO
        to ACTIVE for an on-demand window without losing intent.
        """
        return self._scan_mode_override or self.requested_mode

    async def _async_start_attempt(self, attempt: int) -> bool:  # noqa: C901
        """Start the scanner and handle errors."""
        assert (  # noqa: S101
            self._loop is not None
        ), "Loop is not set, call async_setup first"

        effective_mode = self._effective_mode()
        radio_mode = (
            _resolve_radio_mode(effective_mode) if effective_mode is not None else None
        )
        self.set_current_mode(radio_mode)
        # 1st attempt - no auto reset
        # 2nd attempt - try to reset the adapter and wait a bit
        # 3th attempt - no auto reset
        # 4th attempt - fallback to passive if available

        if (
            IS_LINUX
            and attempt == START_ATTEMPTS
            and radio_mode is BluetoothScanningMode.ACTIVE
        ):
            _LOGGER.debug(
                "%s: Falling back to passive scanning mode "
                "after active scanning failed (%s/%s)",
                self.name,
                attempt,
                START_ATTEMPTS,
            )
            self.set_current_mode(BluetoothScanningMode.PASSIVE)

        assert self.current_mode is not None  # noqa: S101
        self.scanner = create_bleak_scanner(
            (
                None
                if self._manager.has_advertising_side_channel
                else self._async_detection_callback
            ),
            self.current_mode,
            self.adapter,
        )
        # If the scanner is already running, trying to start it again
        # can result in a deadlock. So we need to stop it first.
        # hci0: Opcode 0x200b failed: -110
        # hci0: start background scanning failed: -110
        # hci0: Controller not accepting commands anymore: ncmd = 0
        # hci0: Injecting HCI hardware error event
        # hci0: hardware error 0x00
        await self._async_force_stop_discovery()
        self._log_start_attempt(attempt)
        self._start_future = self._loop.create_future()
        try:
            async with (
                asyncio.timeout(START_TIMEOUT),
                async_interrupt.interrupt(self._start_future, _AbortStartError, None),
            ):
                await self.scanner.start()
        except _AbortStartError as ex:
            await self._async_stop_scanner()
            self._raise_for_abort_start(ex)
        except InvalidMessageError as ex:
            await self._async_stop_scanner()
            self._raise_for_invalid_dbus_message(ex)
        except BrokenPipeError as ex:
            await self._async_stop_scanner()
            self._raise_for_broken_pipe_error(ex)
        except FileNotFoundError as ex:
            await self._async_stop_scanner()
            self._raise_for_file_not_found_error(ex)
        except TimeoutError as ex:
            await self._async_stop_scanner()
            if attempt == 2:
                await self._async_reset_adapter(False)
            if attempt < START_ATTEMPTS:
                self._log_start_timeout(attempt)
                return False
            msg = (
                f"{self.name}: Timed out starting Bluetooth after"
                f" {START_TIMEOUT} seconds; "
                "Try power cycling the Bluetooth hardware."
            )
            raise ScannerStartError(msg) from ex
        except BleakError as ex:
            await self._async_stop_scanner()
            error_str = str(ex)
            if IN_PROGRESS_ERROR in error_str:
                # If discovery is stuck on, try to force stop it
                await self._async_force_stop_discovery()
            if attempt == 2 and _error_indicates_reset_needed(error_str):
                await self._async_reset_adapter(False)
            elif (
                attempt != START_ATTEMPTS
                and _error_indicates_wait_for_adapter_to_init(error_str)
            ):
                # If we are not out of retry attempts, and the
                # adapter is still initializing, wait a bit and try again.
                self._log_adapter_init_wait(attempt)
                await asyncio.sleep(ADAPTER_INIT_TIME)
            if attempt < START_ATTEMPTS:
                self._log_start_failed(ex, attempt)
                return False
            msg = (
                f"{self.name}: Failed to start Bluetooth: {ex}; "
                "Try power cycling the Bluetooth hardware."
            )
            raise ScannerStartError(msg) from ex
        except BaseException:
            await self._async_stop_scanner()
            raise
        finally:
            self._start_future = None

        self._log_start_success(attempt, radio_mode)
        self._on_start_success()
        return True

    def _log_adapter_init_wait(self, attempt: int) -> None:
        _LOGGER.debug(
            "%s: Waiting for adapter to initialize; attempt (%s/%s)",
            self.name,
            attempt,
            START_ATTEMPTS,
        )

    def _log_start_success(
        self, attempt: int, radio_mode: BluetoothScanningMode | None
    ) -> None:
        # Compare against the resolved radio mode we *tried* to start
        # in rather than requested_mode: an AUTO scanner mid-active-
        # window has requested_mode=AUTO but radio_mode=ACTIVE, and we
        # don't want to warn "fell back to passive" when the active
        # restart actually succeeded.
        if self.current_mode is not radio_mode:
            _LOGGER.warning(
                "%s: Successful fall-back to passive scanning mode "
                "after active scanning failed (%s/%s)",
                self.name,
                attempt,
                START_ATTEMPTS,
            )
        _LOGGER.debug(
            "%s: Success while starting bluetooth; attempt: (%s/%s)",
            self.name,
            attempt,
            START_ATTEMPTS,
        )

    def _log_start_timeout(self, attempt: int) -> None:
        _LOGGER.debug(
            "%s: TimeoutError while starting bluetooth; attempt: (%s/%s)",
            self.name,
            attempt,
            START_ATTEMPTS,
        )

    def _log_start_failed(self, ex: BleakError, attempt: int) -> None:
        _LOGGER.debug(
            "%s: BleakError while starting bluetooth; attempt: (%s/%s): %s",
            self.name,
            attempt,
            START_ATTEMPTS,
            ex,
            exc_info=ex,
        )

    def _log_start_attempt(self, attempt: int) -> None:
        _LOGGER.debug(
            "%s: Starting bluetooth discovery attempt: (%s/%s)",
            self.name,
            attempt,
            START_ATTEMPTS,
        )

    def _raise_for_abort_start(self, ex: _AbortStartError) -> None:
        _LOGGER.debug(
            "%s: Starting bluetooth scanner aborted: %s",
            self.name,
            ex,
            exc_info=ex,
        )
        msg = f"{self.name}: Starting bluetooth scanner aborted"
        raise ScannerStartError(msg) from ex

    def _raise_for_file_not_found_error(self, ex: FileNotFoundError) -> None:
        _LOGGER.debug(
            "%s: FileNotFoundError while starting bluetooth: %s",
            self.name,
            ex,
            exc_info=ex,
        )
        if is_docker_env():
            msg = (
                f"{self.name}: DBus service not found; docker config may "
                "be missing `-v /run/dbus:/run/dbus:ro`: {ex}"
            )
            raise ScannerStartError(msg) from ex
        msg = (
            f"{self.name}: DBus service not found; make sure the DBus socket "
            f"is available: {ex}"
        )
        raise ScannerStartError(msg) from ex

    def _raise_for_broken_pipe_error(self, ex: BrokenPipeError) -> None:
        """Raise a ScannerStartError for a BrokenPipeError."""
        _LOGGER.debug("%s: DBus connection broken: %s", self.name, ex, exc_info=ex)
        if is_docker_env():
            msg = (
                f"{self.name}: DBus connection broken: {ex}; try restarting "
                "`bluetooth`, `dbus`, and finally the docker container"
            )
        else:
            msg = (
                f"{self.name}: DBus connection broken: {ex}; try restarting "
                "`bluetooth` and `dbus`"
            )
        raise ScannerStartError(msg) from ex

    def _raise_for_invalid_dbus_message(self, ex: InvalidMessageError) -> None:
        """Raise a ScannerStartError for an InvalidMessageError."""
        _LOGGER.debug(
            "%s: Invalid DBus message received: %s",
            self.name,
            ex,
            exc_info=ex,
        )
        msg = f"{self.name}: Invalid DBus message received: {ex}; try restarting `dbus`"
        raise ScannerStartError(msg) from ex

    def _describe_side_channel_state(self) -> str:
        """Summarize where this scanner expects advertisements to come from."""
        manager = self._manager
        idx = self.adapter_idx
        if idx is None:
            return "no adapter_idx; bleak detection_callback path"
        if not manager.has_advertising_side_channel:
            return "MGMT side channel unavailable; bleak detection_callback path"
        registered = manager._side_channel_scanners.get(idx)
        if registered is None:
            return f"MGMT side channel up but hci{idx} unregistered"
        if registered is not self:
            return f"MGMT side channel at hci{idx} bound to a different scanner"
        mgmt_ctl = manager._mgmt_ctl
        protocol = getattr(mgmt_ctl, "protocol", None) if mgmt_ctl else None
        if protocol is None:
            return f"MGMT side channel registered at hci{idx} but protocol down"
        if protocol.transport is None:
            return f"MGMT side channel registered at hci{idx} but transport closed"
        return f"MGMT side channel feeding hci{idx}"

    def _async_scanner_watchdog(self) -> None:
        """Check if the scanner is running."""
        if not self._async_watchdog_triggered():
            return
        if self._start_stop_lock.locked():
            _LOGGER.debug(
                "%s: Scanner is already restarting, deferring restart",
                self.name,
            )
            return
        _LOGGER.debug(
            "%s: Bluetooth scanner has gone quiet for %ss (%s), restarting",
            self.name,
            self.time_since_last_detection(),
            self._describe_side_channel_state(),
        )
        # Immediately mark the scanner as not scanning
        # since the restart task will have to wait for the lock
        self.scanning = False
        self._create_background_task(self._async_restart_scanner())

    async def _async_restart_scanner(self) -> None:
        """Restart the scanner."""
        async with self._start_stop_lock:
            # Stop the scanner but not the watchdog
            # since we want to try again later if it's still quiet
            await self._async_stop_scanner()
            # If there have not been any valid advertisements,
            # or the watchdog has hit the failure path multiple times,
            # do the reset.
            if (
                self._start_time == self._last_detection
                or self.time_since_last_detection() > SCANNER_WATCHDOG_MULTIPLE
            ):
                await self._async_reset_adapter(True)
            try:
                await self._async_start()
            except ScannerStartError:
                _LOGGER.exception(
                    "%s: Failed to restart Bluetooth scanner",
                    self.name,
                )

    async def _async_reset_adapter(self, gone_silent: bool) -> None:
        """Reset the adapter."""
        # There is currently nothing the user can do to fix this
        # so we log at debug level. If we later come up with a repair
        # strategy, we will change this to raise a repair issue as well.
        _LOGGER.debug("%s: adapter stopped responding; executing reset", self.name)
        result = await async_reset_adapter(self.adapter, self.mac_address, gone_silent)
        _LOGGER.debug("%s: adapter reset result: %s", self.name, result)

    async def async_stop(self) -> None:
        """Stop bluetooth scanner."""
        if self._start_future is not None and not self._start_future.done():
            self._start_future.set_exception(_AbortStartError())
        async with self._start_stop_lock:
            self._clear_active_window_state()
            self._async_stop_scanner_watchdog()
            await self._async_stop_scanner()

    def _clear_active_window_state(self) -> None:
        """Reset AUTO active-window state (caller must hold start/stop lock)."""
        if self._active_window_handle is not None:
            self._active_window_handle.cancel()
            self._active_window_handle = None
        self._scan_mode_override = None
        self._active_window_end = 0.0

    def _arm_active_window_timer_if_extends(self, duration: float) -> None:
        """
        Re-arm the timer only if the new duration extends the window.

        Shorter callers no-op so they can't shrink a window another
        caller is depending on.
        """
        if TYPE_CHECKING:
            assert self._loop is not None
        if self._loop.time() + duration > self._active_window_end:
            self._arm_active_window_timer(duration)

    def _arm_active_window_timer(self, duration: float) -> None:
        """
        Schedule the end-of-window callback.

        Stores ``_active_window_end`` from ``loop.time()`` at arming
        time so it matches the real ``call_later`` fire time (a
        pre-restart snapshot would let a shorter follow-up masquerade
        as an extension). Cancels any existing handle first to avoid
        leaking a pending timer.
        """
        if TYPE_CHECKING:
            assert self._loop is not None
        if self._active_window_handle is not None:
            self._active_window_handle.cancel()
        self._active_window_end = self._loop.time() + duration
        self._active_window_handle = self._loop.call_later(
            duration, self._schedule_end_active_window
        )

    async def async_request_active_window(self, duration: float) -> bool:
        """
        Run an active scan for ``duration`` seconds then restore prior mode.

        No-op on non-AUTO scanners. On macOS AUTO is permanent active
        (no passive mode in CoreBluetooth), so a no-op success there.
        Concurrent / repeat callers while a window is open: a longer
        follow-up extends the timer; a shorter follow-up is a no-op
        on the timer but still returns True. No second restart fires.
        Rejects non-finite or non-positive ``duration`` so a stray
        NaN/inf can't poison ``loop.call_later`` or the extension
        comparison; the scheduler clamps before calling but other
        callers (subclasses, tests) may not.
        """
        if self.requested_mode is not BluetoothScanningMode.AUTO:
            return False
        if not math.isfinite(duration) or duration <= 0.0:
            _LOGGER.warning(
                "%s: refusing active window with invalid duration %r",
                self.name,
                duration,
            )
            return False
        if IS_MACOS:
            return True
        if TYPE_CHECKING:
            assert self._loop is not None
        if self._active_window_handle is not None:
            self._arm_active_window_timer_if_extends(duration)
            return True
        async with self._start_stop_lock:
            self._scan_mode_override = BluetoothScanningMode.ACTIVE
            # If the scanner is still ACTIVE here, the end-of-window task
            # for the previous timer is queued but hasn't run yet (it
            # would have cleared current_mode to PASSIVE). Skip the
            # restart; same extend-only rule as the lockless fast path.
            if self.current_mode is BluetoothScanningMode.ACTIVE:
                self._arm_active_window_timer_if_extends(duration)
                return True
            if IS_LINUX:
                entered = await self._async_begin_active_window_via_toggle()
            else:
                entered = await self._async_begin_active_window_via_restart()
            if not entered:
                return False
            self._arm_active_window_timer(duration)
        return True

    async def _async_begin_active_window_via_toggle(self) -> bool:
        """
        Cheap Linux/BlueZ entry via in-place ``_scanning_mode`` flip.

        Caller holds ``_start_stop_lock`` and has set the override.
        On failure clears the override and recovers via a full
        restart so the scanner isn't left stopped.
        """
        try:
            flipped = await self._async_toggle_active_window_mode()
        except BaseException:
            # Any error (CancelledError, SystemExit, leaked BleakError,
            # etc.) must not leave the override stuck at ACTIVE for
            # the next start. Clear and re-raise.
            self._scan_mode_override = None
            raise
        if not flipped:
            return await self._async_abort_active_window()
        return True

    async def _async_begin_active_window_via_restart(self) -> bool:
        """
        Non-Linux entry via full stop+recreate+start in ACTIVE mode.

        Caller holds ``_start_stop_lock`` and has set the override so
        the fresh BleakScanner is constructed in ACTIVE. On
        ScannerStartError or the Linux 4th-attempt PASSIVE fallback
        the override is cleared and False is returned.
        """
        try:
            await self._async_stop_then_start_under_lock()
        except ScannerStartError:
            return await self._async_abort_active_window()
        except BaseException:
            self._scan_mode_override = None
            raise
        if self.current_mode is not BluetoothScanningMode.ACTIVE:
            self._scan_mode_override = None
            return False
        return True

    async def _async_abort_active_window(self) -> bool:
        """
        Roll back a failed active-window entry.

        Clears the ACTIVE override and runs a best-effort
        stop+restart so the scanner comes back up in its underlying
        AUTO/passive mode rather than being left stopped. Returns
        False so callers can ``return await self._async_abort_...``.
        """
        self._scan_mode_override = None
        with contextlib.suppress(ScannerStartError):
            await self._async_stop_then_start_under_lock()
        return False

    def _schedule_end_active_window(self) -> None:
        """Spawn the end-of-window restart task."""
        self._active_window_handle = None
        self._create_background_task(self._async_end_active_window())

    async def _async_end_active_window(self) -> None:
        """Restore the scanner to its underlying mode after the window ends."""
        async with self._start_stop_lock:
            if self._active_window_handle is not None:
                # A new window took over; let it own the override and timer.
                return
            self._scan_mode_override = None
            if not self.scanning:
                return
            if IS_LINUX and await self._async_toggle_active_window_mode():
                return
            # Non-Linux backend, or toggle failed; full restart so we
            # don't leave the scanner stuck in ACTIVE.
            try:
                await self._async_stop_then_start_under_lock()
            except ScannerStartError as ex:
                _LOGGER.warning(
                    "%s: Failed to restart scanner after active window: %s",
                    self.name,
                    ex,
                )

    async def _async_stop_then_start_under_lock(self) -> None:
        """
        Stop and restart the BleakScanner; caller holds _start_stop_lock.

        Full teardown: nulls ``self.scanner`` and constructs a fresh
        one. AUTO active-window flips on Linux use
        ``_async_toggle_active_window_mode`` instead to skip the dbus
        setup + ``restore_discoveries`` cost.
        """
        await self._async_stop_scanner()
        await self._async_start()

    async def _async_toggle_active_window_mode(self) -> bool:
        """
        Toggle the existing BleakScanner between active and passive.

        Stops the live ``self.scanner``, mutates its private
        ``_backend._scanning_mode`` to the value from
        ``_effective_mode()``, restarts the same instance. Skips the
        new dbus client + ``restore_discoveries`` cost of a fresh
        construction; bleak's device cache survives same-instance
        stop+start so ``BleakClient(address)`` keeps working.

        Linux/BlueZ only — callers must check ``IS_LINUX``. Returns
        False if the scanner is gone or stop/start raised (caller
        falls back to the full path).
        """
        if self.scanner is None:
            return False
        effective_mode = self._effective_mode()
        if TYPE_CHECKING:
            assert effective_mode is not None
        radio_mode = _resolve_radio_mode(effective_mode)
        mode_str = SCANNING_MODE_TO_BLEAK[radio_mode]
        try:
            async with asyncio.timeout(STOP_TIMEOUT):
                await self.scanner.stop()
        except (TimeoutError, BleakError) as ex:
            _LOGGER.warning(
                "%s: Error stopping scanner during active-window flip: %s",
                self.name,
                ex,
            )
            # The bleak scanner may be in an undefined state; mark
            # the wrapper not-scanning so the caller's fallback path
            # treats it as stopped.
            self.scanning = False
            return False
        # Private bleak attribute — no public API for mode change.
        # BlueZ reads it on every start; macOS isn't reachable here.
        # Guarded so a future bleak refactor that renames/drops the
        # attribute can't leave the scanner stopped with no restart;
        # caller falls back to the full stop+recreate+start path.
        try:
            self.scanner._backend._scanning_mode = mode_str
        except AttributeError as ex:
            _LOGGER.warning(
                "%s: bleak _backend._scanning_mode unavailable; "
                "cannot toggle in place: %s",
                self.name,
                ex,
            )
            self.scanning = False
            return False
        try:
            async with asyncio.timeout(START_TIMEOUT):
                await self.scanner.start()
        except (TimeoutError, BleakError, ScannerStartError) as ex:
            _LOGGER.warning(
                "%s: Error starting scanner during active-window flip: %s",
                self.name,
                ex,
            )
            # Scanner was stopped above and didn't come back; mark
            # not-scanning so it matches reality.
            self.scanning = False
            return False
        self.scanning = True
        self.set_current_mode(radio_mode)
        return True

    async def _async_stop_scanner(self) -> None:
        """Stop bluetooth discovery under the lock."""
        self.scanning = False
        if self.scanner is None:
            _LOGGER.debug("%s: Scanner is already stopped", self.name)
            return
        _LOGGER.debug("%s: Stopping bluetooth discovery", self.name)
        try:
            async with asyncio.timeout(STOP_TIMEOUT):
                await self.scanner.stop()
        except (TimeoutError, BleakError):
            # This is not fatal, and they may want to reload
            # the config entry to restart the scanner if they
            # change the bluetooth dongle.
            _LOGGER.exception("%s: Error stopping scanner", self.name)
        self.scanner = None

    async def _async_force_stop_discovery(self) -> None:
        """Force stop discovery."""
        _LOGGER.debug("%s: Force stopping bluetooth discovery", self.name)
        try:
            async with asyncio.timeout(STOP_TIMEOUT):
                await stop_discovery(self.adapter)
        except TimeoutError:
            _LOGGER.exception("%s: Timeout force stopping scanner", self.name)
        except Exception:
            # Best-effort BlueZ cleanup; dbus_fast can raise a wide
            # variety of errors and we don't want any of them to
            # propagate out of the recovery path.
            _LOGGER.exception("%s: Failed to force stop scanner", self.name)
