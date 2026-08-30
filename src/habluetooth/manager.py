"""The bluetooth integration."""

from __future__ import annotations

import asyncio
import itertools
import logging
import math
import platform
from dataclasses import asdict
from functools import partial
from typing import TYPE_CHECKING, Any

from bleak_retry_connector import (
    NO_RSSI_VALUE,
    AllocationChangeEvent,
    Allocations,
    BleakSlotManager,
)
from bluetooth_adapters import (
    ADAPTER_ADDRESS,
    ADAPTER_PASSIVE_SCAN,
    AdapterDetails,
    BluetoothAdapters,
    get_adapters,
)
from bluetooth_data_tools import monotonic_time_coarse

from .advertisement_tracker import (
    TRACKER_BUFFERING_WOBBLE_SECONDS,
    AdvertisementTracker,
)
from .auto_scheduler import ActiveScanRequest, AutoScanScheduler
from .channels.bluez import CONNECTION_ERRORS, MGMTBluetoothCtl
from .const import (
    ADV_RSSI_SWITCH_DEADBAND,
    ADV_RSSI_SWITCH_THRESHOLD,
    CALLBACK_TYPE,
    CLIENT_DISCONNECT_TIMEOUT,
    DEFAULT_ACTIVE_SCAN_DURATION,
    DEFAULT_ACTIVE_SCAN_INTERVAL,
    DEFAULT_ON_DEMAND_SWEEP_DURATION,
    DURABLY_GONE_STALE_FACTOR,
    FAILED_ADAPTER_MAC,
    FALLBACK_MAXIMUM_STALE_ADVERTISEMENT_SECONDS,
    MIN_ACTIVE_SCAN_DURATION,
    MIN_ACTIVE_SCAN_INTERVAL,
    RESCUE_SCAN_RETRY_SECONDS,
    RSSI_SMOOTHING_FACTOR,
    STALE_ROAM_FACTOR,
    STRONG_OWNER_STALE_RSSI,
    UNAVAILABLE_TRACK_SECONDS,
)
from .models import (
    BluetoothReachabilityIntent,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
    HaBluetoothSlotAllocations,
    HaScannerModeChange,
    HaScannerRegistration,
    HaScannerRegistrationEvent,
)
from .scanner_device import BluetoothScannerDevice
from .usage import install_multiple_bleak_catcher, uninstall_multiple_bleak_catcher
from .util import async_reset_adapter, coalesce_concurrent_future

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Iterable

    from bleak.backends.device import BLEDevice
    from bleak.backends.scanner import AdvertisementData, AdvertisementDataCallback

    from .base_scanner import BaseHaScanner
    from .wrappers import HaBleakClientWrapper


SYSTEM = platform.system()
IS_LINUX = SYSTEM == "Linux"

# No Final on globals declared in the .pxd; Cython 3.3 crashes on it
# (cython/cython#7942).
FILTER_UUIDS = "UUIDs"

APPLE_MFR_ID = 76
APPLE_IBEACON_START_BYTE = 0x02  # iBeacon (tilt_ble)
APPLE_HOMEKIT_START_BYTE = 0x06  # homekit_controller
APPLE_DEVICE_ID_START_BYTE = 0x10  # bluetooth_le_tracker
APPLE_HOMEKIT_NOTIFY_START_BYTE = 0x11  # homekit_controller
APPLE_FINDMY_START_BYTE = 0x12  # FindMy network advertisements


_str = str
_int = int

# Hot-path C copies of the public constants (declared cdef in the .pxd);
# the public names stay patchable Python constants.
_DURABLY_GONE_STALE_FACTOR = DURABLY_GONE_STALE_FACTOR
_STRONG_OWNER_STALE_RSSI = STRONG_OWNER_STALE_RSSI
_RSSI_SMOOTHING_FACTOR = RSSI_SMOOTHING_FACTOR
_ADV_RSSI_SWITCH_DEADBAND = ADV_RSSI_SWITCH_DEADBAND
_RESCUE_SCAN_RETRY_SECONDS = RESCUE_SCAN_RETRY_SECONDS
_STALE_ROAM_FACTOR = STALE_ROAM_FACTOR

# Shared empty set used as the default for the reclaim-hysteresis lookup, so the
# hot path skips a None check and never allocates a throwaway set.
_EMPTY_DEMOTED: frozenset[str] = frozenset()

_LOGGER = logging.getLogger(__name__)


def _dispatch_bleak_callback(
    bleak_callback: BleakCallback,
    device: BLEDevice,
    advertisement_data: AdvertisementData,
) -> None:
    """Dispatch the callback."""
    if (
        uuids := bleak_callback.filters.get(FILTER_UUIDS)
    ) is not None and not uuids.intersection(advertisement_data.service_uuids):
        return

    try:
        bleak_callback.callback(device, advertisement_data)
    except Exception:  # pylint: disable=broad-except
        _LOGGER.exception("Error in callback: %s", bleak_callback.callback)


def _zeroed_allocations(source: str) -> HaBluetoothSlotAllocations:
    """
    Return an empty allocations snapshot for a source.

    ``slots=0`` is the established sentinel for "no slot information
    reported": non-connectable scanners have always been seeded with it,
    and every consumer that reasons about exhaustion filters on
    ``slots > 0``.
    """
    return HaBluetoothSlotAllocations(source=source, slots=0, free=0, allocated=[])


class BleakCallback:
    """Bleak callback."""

    __slots__ = ("callback", "filters")

    def __init__(
        self, callback: AdvertisementDataCallback, filters: dict[str, set[str]]
    ) -> None:
        """Init bleak callback."""
        self.callback = callback
        self.filters = filters


class BluetoothManager:
    """Manage Bluetooth."""

    __slots__ = (
        "_adapter_refresh_future",
        "_adapter_sources",
        "_adapters",
        "_advertisement_tracker",
        "_all_history",
        "_allocations",
        "_allocations_callbacks",
        "_auto_scheduler",
        "_background_tasks",
        "_bleak_callbacks",
        "_bluetooth_adapters",
        "_cancel_allocation_callbacks",
        "_cancel_unavailable_tracking",
        "_connectable_history",
        "_connectable_scanners",
        "_connectable_unavailable_callbacks",
        "_connection_history",
        "_debug",
        "_demoted_sources",
        "_disappeared_callbacks",
        "_fallback_intervals",
        "_intervals",
        "_loop",
        "_mgmt_ctl",
        "_name_cache",
        "_non_connectable_scanners",
        "_recovery_lock",
        "_rescue_triggered",
        "_scanner_mode_change_callbacks",
        "_scanner_registration_callbacks",
        "_side_channel_scanners",
        "_smoothed_rssi",
        "_sources",
        "_subclass_discover_info",
        "_unavailable_callbacks",
        "_warned_passive_active_scan",
        "has_advertising_side_channel",
        "shutdown",
        "slot_manager",
    )

    def __init__(
        self,
        bluetooth_adapters: BluetoothAdapters | None = None,
        slot_manager: BleakSlotManager | None = None,
    ) -> None:
        """Init bluetooth manager."""
        self._cancel_unavailable_tracking: asyncio.TimerHandle | None = None

        self._advertisement_tracker = AdvertisementTracker()
        self._fallback_intervals = self._advertisement_tracker.fallback_intervals
        self._intervals = self._advertisement_tracker.intervals

        self._unavailable_callbacks: dict[
            str, set[Callable[[BluetoothServiceInfoBleak], None]]
        ] = {}
        self._connectable_unavailable_callbacks: dict[
            str, set[Callable[[BluetoothServiceInfoBleak], None]]
        ] = {}

        self._bleak_callbacks: set[BleakCallback] = set()
        self._all_history: dict[str, BluetoothServiceInfoBleak] = {}
        self._connectable_history: dict[str, BluetoothServiceInfoBleak] = {}
        # address -> source -> EWMA-smoothed advertisement RSSI. Only
        # populated for addresses seen from more than one source (the
        # arbitration that uses it only runs cross-source); single-proxy
        # devices never allocate a bucket. Evicted with the device.
        self._smoothed_rssi: dict[str, dict[str, float]] = {}
        # address -> the set of sources currently in the demoted state (each one
        # lost ownership and is not the current owner). Used for asymmetric
        # switch hysteresis: a challenger reclaiming ownership it recently lost
        # must clear an extra deadband, so a stationary device stops
        # ping-ponging between similar-signal proxies; a genuine one-way move to
        # a source that has never recently owned the device is not in the set
        # and pays nothing. Holding the whole set (not just the most-recent
        # loser) damps N-way contention too: in an A->B->C->A bounce A is still
        # in the set when it reclaims from C, so it is charged the deadband. The
        # current owner is never in its own set (removed on each win), so the
        # set is self-bounded by the proxy count. Evicted with the device and
        # per source on unregister.
        self._demoted_sources: dict[str, set[str]] = {}
        # address -> monotonic time a rescue-scan episode started for a
        # device that needs active scans (issue #591). When the stale
        # handoff is denied for such a device, an active window is
        # triggered on both the owner's and the challenger's scanners
        # instead of pinning ownership; the switch is deferred until the
        # challenger's advertisement postdates the rescue's accept time
        # (see _rescue_stale_handoff). The owner being heard again
        # invalidates the episode (old.time >= trigger), so a stale
        # record can never authorize a later instant switch. Evicted
        # with the device; an entry orphaned by its active-scan need being
        # unregistered is cleared on the device's next stale arbitration.
        self._rescue_triggered: dict[str, float] = {}
        # Cross-scanner name cache: address -> best name seen across all
        # scanners. Passive scanners typically miss the device name because
        # it lives in SCAN_RSP (active-only); the cache lets a name learned
        # by an active scanner flow to passive scanners' service_info on
        # dispatch. Updates use the case-folded prefix-extension rule: a
        # longer name only replaces a shorter cached one when the cached
        # one is a case-folded prefix; otherwise the new name is treated
        # as a rename and replaces unconditionally.
        self._name_cache: dict[str, str] = {}
        self._non_connectable_scanners: set[BaseHaScanner] = set()
        self._connectable_scanners: set[BaseHaScanner] = set()
        self._adapters: dict[str, AdapterDetails] = {}
        self._adapter_sources: dict[str, str] = {}
        self._allocations: dict[str, HaBluetoothSlotAllocations] = {}
        self._sources: dict[str, BaseHaScanner] = {}
        self._bluetooth_adapters = bluetooth_adapters or get_adapters()
        self.slot_manager = slot_manager or BleakSlotManager()
        self._cancel_allocation_callbacks = (
            self.slot_manager.register_allocation_callback(
                self._async_slot_manager_changed
            )
        )
        self._debug = _LOGGER.isEnabledFor(logging.DEBUG)
        self.shutdown = False
        self._background_tasks: set[asyncio.Task[None]] = set()
        self.has_advertising_side_channel = False
        self._side_channel_scanners: dict[int, BaseHaScanner] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._adapter_refresh_future: asyncio.Future[None] | None = None
        self._recovery_lock: asyncio.Lock = asyncio.Lock()
        self._disappeared_callbacks: set[Callable[[str], None]] = set()
        self._allocations_callbacks: dict[
            str | None, set[Callable[[HaBluetoothSlotAllocations], None]]
        ] = {}
        self._scanner_registration_callbacks: dict[
            str | None, set[Callable[[HaScannerRegistration], None]]
        ] = {}
        self._scanner_mode_change_callbacks: dict[
            str | None, set[Callable[[HaScannerModeChange], None]]
        ] = {}
        # Sources of passive-only scanners we've already warned about
        # while active scans are requested; deduped so one passive proxy
        # behind many devices warns once.
        self._warned_passive_active_scan: set[str] = set()
        self._subclass_discover_info = self._discover_service_info
        self._mgmt_ctl: MGMTBluetoothCtl | None = None
        self._auto_scheduler = AutoScanScheduler(self)
        if (
            self._discover_service_info.__func__  # type: ignore[attr-defined]
            is BluetoothManager._discover_service_info
        ):
            _LOGGER.warning(
                "%s: does not implement _discover_service_info, "
                "subclasses must implement this method to consume "
                "discovery data",
                type(self).__name__,
            )

    @property
    def supports_passive_scan(self) -> bool:
        """Return if passive scan is supported."""
        return any(adapter[ADAPTER_PASSIVE_SCAN] for adapter in self._adapters.values())

    def supports_passive_scan_for(self, adapter: str) -> bool:
        """Return if passive scan is supported on a specific adapter."""
        return self._adapters.get(adapter, {}).get(ADAPTER_PASSIVE_SCAN, False)

    def is_operating_degraded(self) -> bool:
        """
        Return if the manager is operating in degraded mode.

        On Linux, we're in degraded mode if mgmt control is not available.
        This typically means we don't have NET_ADMIN/NET_RAW capabilities.
        """
        return IS_LINUX and self._mgmt_ctl is None

    def on_scanner_start(self, scanner: BaseHaScanner) -> None:
        """
        Called when a scanner starts.

        Subclasses can override this to perform custom actions when a scanner starts.
        """

    def async_scanner_count(self, connectable: bool = True) -> int:
        """Return the number of scanners."""
        if connectable:
            return len(self._connectable_scanners)
        return len(self._connectable_scanners) + len(self._non_connectable_scanners)

    async def async_diagnostics(self) -> dict[str, Any]:
        """Diagnostics for the manager."""
        scanner_diagnostics = await asyncio.gather(
            *[
                scanner.async_diagnostics()
                for scanner in itertools.chain(
                    self._non_connectable_scanners, self._connectable_scanners
                )
            ]
        )
        return {
            "adapters": self._adapters,
            "slot_manager": self.slot_manager.diagnostics(),
            "allocations": {
                source: asdict(allocations)
                for source, allocations in self._allocations.items()
            },
            "scanners": scanner_diagnostics,
            "connectable_history": [
                service_info.as_dict()
                for service_info in self._connectable_history.values()
            ],
            "all_history": [
                service_info.as_dict() for service_info in self._all_history.values()
            ],
            "advertisement_tracker": self._advertisement_tracker.async_diagnostics(),
            "auto_scheduler": self._auto_scheduler.async_diagnostics(),
        }

    def _find_adapter_by_address(self, address: str) -> str | None:
        for adapter, details in self._adapters.items():
            if details[ADAPTER_ADDRESS] == address:
                return adapter
        return None

    def async_scanner_by_source(self, source: str) -> BaseHaScanner | None:
        """Return the scanner for a source."""
        return self._sources.get(source)

    def async_register_disappeared_callback(
        self, callback: Callable[[str], None]
    ) -> CALLBACK_TYPE:
        """Register a callback to be called when an address disappears."""
        self._disappeared_callbacks.add(callback)
        return partial(self._disappeared_callbacks.discard, callback)

    @coalesce_concurrent_future("_adapter_refresh_future")
    async def _async_refresh_adapters(self) -> None:
        """Refresh the adapters."""
        await self._bluetooth_adapters.refresh()
        self._adapters = self._bluetooth_adapters.adapters

    def get_cached_bluetooth_adapters(self) -> dict[str, AdapterDetails] | None:
        """Get cached bluetooth adapters synchronously."""
        return self._adapters

    async def async_get_bluetooth_adapters(
        self, cached: bool = True
    ) -> dict[str, AdapterDetails]:
        """Get bluetooth adapters."""
        if not self._adapters or not cached:
            if not cached:
                await self._async_refresh_adapters()
            self._adapters = self._bluetooth_adapters.adapters
        return self._adapters

    async def async_get_adapter_from_address(self, address: str) -> str | None:
        """Get adapter from address."""
        if adapter := self._find_adapter_by_address(address):
            return adapter
        await self._async_refresh_adapters()
        return self._find_adapter_by_address(address)

    async def async_get_adapter_from_address_or_recover(
        self, address: str
    ) -> str | None:
        """Get adapter from address or recover."""
        if adapter := self._find_adapter_by_address(address):
            return adapter
        await self._async_recover_failed_adapters()
        return self._find_adapter_by_address(address)

    async def _async_recover_failed_adapters(self) -> None:
        """Recover failed adapters."""
        if self._recovery_lock.locked():
            # Already recovering, no need to
            # start another recovery
            return
        async with self._recovery_lock:
            adapters = await self.async_get_bluetooth_adapters()
            for adapter in [
                adapter
                for adapter, details in adapters.items()
                if details[ADAPTER_ADDRESS] == FAILED_ADAPTER_MAC
            ]:
                await async_reset_adapter(adapter, FAILED_ADAPTER_MAC, False)
            await self._async_refresh_adapters()

    async def async_setup(self) -> None:
        """Set up the bluetooth manager."""
        # Deferred to avoid the circular import that a top-level
        # ``from .central_manager import CentralBluetoothManager``
        # would create (central_manager itself imports BluetoothManager
        # under TYPE_CHECKING but only this method writes through it).
        from .central_manager import CentralBluetoothManager  # noqa: PLC0415

        if CentralBluetoothManager.manager is None:
            CentralBluetoothManager.manager = self
        self._loop = asyncio.get_running_loop()
        await self._async_refresh_adapters()
        install_multiple_bleak_catcher()
        self.async_setup_unavailable_tracking()
        self._auto_scheduler.start(self._loop)
        if not IS_LINUX:
            return
        self._mgmt_ctl = MGMTBluetoothCtl(10.0, self._side_channel_scanners)
        try:
            await self._mgmt_ctl.setup()
        except PermissionError:
            _LOGGER.exception(
                "Missing required permissions for Bluetooth management. "
                "Automatic adapter recovery is unavailable. "
                "Add NET_ADMIN and NET_RAW capabilities to the container to enable it"
            )
            self._mgmt_ctl = None
        except CONNECTION_ERRORS as ex:
            _LOGGER.debug("Cannot start Bluetooth Management API: %s", ex)
            self._mgmt_ctl = None
        else:
            self.has_advertising_side_channel = True

    def async_stop(self) -> None:
        """Stop the Bluetooth integration at shutdown."""
        _LOGGER.debug("Stopping bluetooth manager")
        self.shutdown = True
        if self._cancel_unavailable_tracking:
            self._cancel_unavailable_tracking.cancel()
            self._cancel_unavailable_tracking = None
        self._auto_scheduler.stop()
        if self._background_tasks:
            # The cancel is fire and forget; log here so an unregister that
            # raced shutdown leaves a trace even if the task never started.
            _LOGGER.debug(
                "Cancelling %d background task(s) at stop: %s",
                len(self._background_tasks),
                [task.get_name() for task in self._background_tasks],
            )
        for task in list(self._background_tasks):
            task.cancel()
        uninstall_multiple_bleak_catcher()
        self._cancel_allocation_callbacks()
        if self._mgmt_ctl:
            self._mgmt_ctl.close()
            self._mgmt_ctl = None

    def async_scanner_devices_by_address(
        self, address: str, connectable: bool
    ) -> list[BluetoothScannerDevice]:
        """Get BluetoothScannerDevice by address."""
        if not connectable:
            scanners: Iterable[BaseHaScanner] = itertools.chain(
                self._connectable_scanners, self._non_connectable_scanners
            )
        else:
            scanners = self._connectable_scanners
        return [
            BluetoothScannerDevice(scanner, *device_adv)
            for scanner in scanners
            if (device_adv := scanner.get_discovered_device_advertisement_data(address))
        ]

    def async_discovered_devices(self, connectable: bool) -> list[BLEDevice]:
        """Return all of combined best path to discovered from all the scanners."""
        histories = self._connectable_history if connectable else self._all_history
        return [history.device for history in histories.values()]

    def async_setup_unavailable_tracking(self) -> None:
        """Set up the unavailable tracking."""
        self._schedule_unavailable_tracking()

    def _schedule_unavailable_tracking(self) -> None:
        """Schedule the unavailable tracking."""
        if TYPE_CHECKING:
            assert self._loop is not None
        loop = self._loop
        self._cancel_unavailable_tracking = loop.call_at(
            loop.time() + UNAVAILABLE_TRACK_SECONDS, self._async_check_unavailable
        )

    def _async_check_unavailable(self) -> None:  # noqa: C901
        """Watch for unavailable devices and cleanup state history."""
        monotonic_now = monotonic_time_coarse()
        connectable_history = self._connectable_history
        all_history = self._all_history
        tracker = self._advertisement_tracker
        intervals = tracker.intervals

        # Materialize each scanner's discovered_addresses exactly once per
        # cycle. For local HaScanner this property rebuilds bleak's
        # discovered-devices dict on every access, so the prior two-pass
        # iteration paid that cost twice for the connectable scanners.
        connectable_addrs: set[str] = set()
        for scanner in self._connectable_scanners:
            connectable_addrs.update(scanner.discovered_addresses)
        all_addrs = connectable_addrs.copy()
        for scanner in self._non_connectable_scanners:
            all_addrs.update(scanner.discovered_addresses)

        for connectable in (True, False):
            if connectable:
                unavailable_callbacks = self._connectable_unavailable_callbacks
            else:
                unavailable_callbacks = self._unavailable_callbacks
            history = connectable_history if connectable else all_history
            disappeared = set(history).difference(
                connectable_addrs if connectable else all_addrs
            )
            for address in disappeared:
                if not connectable:
                    #
                    # For non-connectable devices we also check the device has exceeded
                    # the advertising interval before we mark it as unavailable
                    # since it may have gone to sleep and since we do not need an active
                    # connection to it we can only determine its availability
                    # by the lack of advertisements
                    if advertising_interval := (
                        intervals.get(address) or self._fallback_intervals.get(address)
                    ):
                        advertising_interval += TRACKER_BUFFERING_WOBBLE_SECONDS
                    else:
                        advertising_interval = (
                            FALLBACK_MAXIMUM_STALE_ADVERTISEMENT_SECONDS
                        )
                    time_since_seen = monotonic_now - all_history[address].time
                    if time_since_seen <= advertising_interval:
                        continue

                    # The second loop (connectable=False) is responsible for removing
                    # the device from all the interval tracking since it is no longer
                    # available for both connectable and non-connectable
                    tracker.async_remove_fallback_interval(address)
                    tracker.async_remove_address(address)
                    self._name_cache.pop(address, None)
                    self._smoothed_rssi.pop(address, None)
                    self._demoted_sources.pop(address, None)
                    self._rescue_triggered.pop(address, None)
                    for disappear_callback in self._disappeared_callbacks:
                        try:
                            disappear_callback(address)
                        except Exception:
                            _LOGGER.exception("Error in disappeared callback")
                    self._address_disappeared(address)

                service_info = history.pop(address)

                if not (callbacks := unavailable_callbacks.get(address)):
                    continue

                for callback in callbacks.copy():
                    try:
                        callback(service_info)
                    except Exception:  # pylint: disable=broad-except
                        _LOGGER.exception("Error in unavailable callback")

        self._schedule_unavailable_tracking()

    def _address_disappeared(self, address: str) -> None:
        """
        Call when an address disappears from the stack.

        This method is intended to be overridden by subclasses.
        """

    def _should_keep_previous_adv(
        self,
        old_info: BluetoothServiceInfoBleak,
        new_info: BluetoothServiceInfoBleak,
        smoothed: dict[_str, float] | None,
        new_rssi: float,
        record_demotion: bool,
    ) -> bool:
        """
        Return True when ``old_info`` should win over ``new_info``.

        Only relevant when ``old_info`` came from a different still-scanning
        source. The ``is not / !=`` ordering is a PyObject_RichCompare
        short-circuit that dominates this hot path; keep it intact. ``smoothed``
        is the address's per-source smoothed-RSSI bucket, which is None until
        the device is seen from more than one source; testing it first lets a
        single-proxy device exit before the source compares and narrows the
        bucket to non-None for the cross-source predicate. ``new_rssi`` is the
        already-computed smoothed value for new_info's source, threaded through
        to avoid re-looking it up. ``record_demotion`` is forwarded to the
        predicate so an RSSI-path switch can remember the demoted owner; it is
        True only for the all-history decision, not the connectable re-check.
        """
        return (
            smoothed is not None
            and new_info.source is not old_info.source
            and new_info.source != old_info.source
            and (scanner := self._sources.get(old_info.source)) is not None
            and scanner.scanning
            and self._prefer_previous_adv_from_different_source(
                old_info, new_info, smoothed, new_rssi, record_demotion
            )
        )

    def _prefer_previous_adv_from_different_source(
        self,
        old: BluetoothServiceInfoBleak,
        new: BluetoothServiceInfoBleak,
        smoothed: dict[_str, float],
        new_rssi: float,
        record_demotion: bool,
    ) -> bool:
        """Prefer previous advertisement from a different source if it is better."""
        # Compare smoothed per-source RSSI so a momentary spike does not flip
        # ownership for a stationary device. ``new_rssi`` is the caller's
        # already-computed smoothed value for new.source; old's smoothed value
        # is looked up here, falling back to its instantaneous RSSI when this
        # source has no smoothed sample yet.
        old_rssi = smoothed.get(old.source, old.rssi or NO_RSSI_VALUE)
        if stale_seconds := self._intervals.get(
            new.address, self._fallback_intervals.get(new.address, 0)
        ):
            stale_seconds += TRACKER_BUFFERING_WOBBLE_SECONDS
        else:
            stale_seconds = FALLBACK_MAXIMUM_STALE_ADVERTISEMENT_SECONDS
        elapsed = new.time - old.time
        if elapsed > stale_seconds and self._stale_challenger_wins(
            old, new, new_rssi, old_rssi, elapsed, stale_seconds, record_demotion
        ):
            return False
        # Only a challenger that clears the plain THRESHOLD can win, so do the
        # cheap compare first and skip the reclaim-state lookup on every
        # arbitration that keeps the owner (the common case).
        if new_rssi - ADV_RSSI_SWITCH_THRESHOLD <= old_rssi:
            return True
        # Asymmetric hysteresis: a challenger reclaiming ownership it recently
        # lost must also clear DEADBAND. This stops a heavily-multipathed
        # stationary device whose smoothed RSSI straddles the threshold from
        # ping-ponging between similar-signal proxies, while a genuine one-way
        # move (never a recent owner) still hands off at THRESHOLD. The empty-set
        # default keeps this a single set membership test with no None check.
        switch_margin = ADV_RSSI_SWITCH_THRESHOLD
        if new.source in self._demoted_sources.get(new.address, _EMPTY_DEMOTED):
            switch_margin += _ADV_RSSI_SWITCH_DEADBAND
        if new_rssi - switch_margin > old_rssi:
            # The new source wins ownership on the active RSSI path. Record the
            # ownership change so a quick reclaim faces the deadband above (only
            # for the all-history decision, and not the stale handoff, so a
            # strong owner recovering from transient silence still reclaims at
            # THRESHOLD).
            if record_demotion:
                self._record_demotion(new.address, new.source, old.source)
            # If new advertisement is switch_margin more, prefer the new one
            # (smoothed RSSI when available).
            if self._debug:
                _LOGGER.debug(
                    "%s (%s): Switching from %s to %s (new rssi:%s - threshold:%s >"
                    " old rssi:%s)",
                    new.name,
                    new.address,
                    self._async_describe_source(old),
                    self._async_describe_source(new),
                    new_rssi,
                    switch_margin,
                    old_rssi,
                )
            return False
        return True

    def _stale_challenger_wins(
        self,
        old: BluetoothServiceInfoBleak,
        new: BluetoothServiceInfoBleak,
        new_rssi: float,
        old_rssi: float,
        elapsed: float,
        stale_seconds: float,
        record_demotion: bool,
    ) -> bool:
        """
        Decide a stale handoff; True hands the device to the challenger.

        The owner has not been heard within its expected interval, so the
        challenger's advertisement is strictly newer than anything the owner
        has produced. What happens next depends on whether the device needs
        active scans (an integration registered an on-demand active-scan
        need via async_register_active_scan because the device's data rides
        SCAN_RSP):

        An owner silent past the durably-gone threshold loses to any
        challenger, for every device class: receive time is all we have
        (adverts carry no timestamp), so the durably-gone wait is what
        lets a device that truly moved into weak-only coverage still hand
        off. Short of that, a challenger of a strong owner must NOT take
        over on a single missed interval: the owner almost certainly
        re-hears the device on its next advertisement and either the
        handoff flaps straight back (a materially stronger reclaim) or
        cannot flap back and ping-pongs on alternating misses (a
        comparable pair) — the stationary-device flap issues #568/#580
        fixed. A materially stronger challenger can still win at any time
        on the smoothed RSSI path below (deadband-protected, stale or
        not); what differs per device class is everything in between:

        * Passive device (no registered need): a comparable-or-stronger
          challenger of a weak owner takes over once the owner has been
          silent STALE_ROAM_FACTOR stale windows (ordinary roaming;
          payloads are identical across scanners, so its capture is as
          good as anyone's, and the extra half window keeps a per-scanner
          reception miss from roaming a stationary device). Anything else
          waits for durably-gone, which costs nothing data-wise for the
          same reason.
        * Active-need device: there is no roaming shortcut at all — even a
          comparable challenger of a weak owner may be an AUTO scanner in
          its passive phase whose capture carries no fresh scan response
          (issue #568). Instead of pinning ownership until the owner is
          durably gone (issue #591), trigger an active window on both the
          owner (a chance to re-hear the device) and the challenger (its
          next capture is a fresh scan response), and hand off on the
          first challenger advertisement past the accept time while the
          owner stayed silent (see _rescue_stale_handoff). Accept times
          always sit RESCUE_SCAN_ACCEPT_SECONDS after coverage so the
          owner is guaranteed one re-hear window before any handoff. An
          owner that paused scanning entirely never reaches this code; its
          devices were handed off at the scanning gate.

        The cheap comparisons decide first; the scheduler lookup that
        classifies the device as active-need runs only when they keep the
        owner, so the ordinary roaming and durably-gone switches never pay
        for it.
        """
        durably_gone = min(
            stale_seconds * _DURABLY_GONE_STALE_FACTOR,
            FALLBACK_MAXIMUM_STALE_ADVERTISEMENT_SECONDS,
        )
        if elapsed > durably_gone:
            # Unconditional backstop for every device class: someone has to
            # own a device whose owner has been silent this long, even if
            # the challenger's capture is not an active one.
            if self._debug:
                _LOGGER.debug(
                    "%s (%s): Switching from %s to %s (time elapsed:%s > stale"
                    " seconds:%s; durably-gone threshold:%s)",
                    new.name,
                    new.address,
                    self._async_describe_source(old),
                    self._async_describe_source(new),
                    elapsed,
                    stale_seconds,
                    durably_gone,
                )
            self._end_rescue_episode(new.address, record_demotion)
            return True
        if self._auto_scheduler._requests_by_address.get(new.address) is None:
            # Passive device: a comparable-or-stronger challenger of a weak
            # owner takes over once the owner has been silent for
            # STALE_ROAM_FACTOR stale windows (its capture is as good as
            # anyone's, payloads are identical across scanners). The extra
            # half window over plain stale means the owner must miss two
            # reception opportunities, not one, before roaming; a single
            # miss is a per-scanner duty-cycle lottery, not evidence the
            # device moved. Anything else waits for durably-gone above.
            # The episode ends either way; it also cleans up after an
            # active-scan need unregistered mid-episode (defense in depth,
            # the unregister itself clears it too).
            self._end_rescue_episode(new.address, record_demotion)
            if (
                elapsed > stale_seconds * _STALE_ROAM_FACTOR
                and new_rssi >= old_rssi - ADV_RSSI_SWITCH_THRESHOLD
                and old_rssi < _STRONG_OWNER_STALE_RSSI
            ):
                if self._debug:
                    _LOGGER.debug(
                        "%s (%s): Switching from %s to %s (time elapsed:%s >"
                        " roam threshold:%s; passive roaming, comparable"
                        " challenger of a weak owner)",
                        new.name,
                        new.address,
                        self._async_describe_source(old),
                        self._async_describe_source(new),
                        elapsed,
                        stale_seconds * _STALE_ROAM_FACTOR,
                    )
                return True
            return False
        if not record_demotion:
            # Connectable re-check: connection routing cares about
            # liveness, not scan-response freshness, so the ordinary
            # roaming rule applies here exactly as it does for a passive
            # device (same STALE_ROAM_FACTOR gate); the connectable
            # history must repoint away from a silent scanner well before
            # durably-gone rather than route a connect attempt at a dead
            # radio. Only the all-history decision drives episode state.
            return (
                elapsed > stale_seconds * _STALE_ROAM_FACTOR
                and new_rssi >= old_rssi - ADV_RSSI_SWITCH_THRESHOLD
                and old_rssi < _STRONG_OWNER_STALE_RSSI
            )
        # Active-need all-history decision: never take the roaming
        # shortcut, even for a comparable challenger of a weak owner; the
        # challenger may be an AUTO scanner sitting in its passive phase
        # whose capture carries no fresh scan response (issue #568).
        # Every handoff before durably-gone goes through the rescue flow,
        # so it lands on a capture from an address a recent active window
        # covered.
        return self._rescue_stale_handoff(old, new, elapsed, stale_seconds)

    def _end_rescue_episode(self, address: str, record_demotion: bool) -> None:
        """
        End any rescue episode on a stale arbitration outside the rescue.

        Runs on the durably-gone handoff and on every passive stale
        arbitration whether or not the device hands off (the passive case
        also cleans up an episode orphaned by an unregistered active-scan
        need). Only the all-history decision (record_demotion) owns
        episode state, and the empty-dict check keeps the common
        no-episode case to a single branch.
        """
        if record_demotion and self._rescue_triggered:
            self._rescue_triggered.pop(address, None)

    def _rescue_stale_handoff(
        self,
        old: BluetoothServiceInfoBleak,
        new: BluetoothServiceInfoBleak,
        elapsed: float,
        stale_seconds: float,
    ) -> bool:
        """
        Advance the rescue episode for a denied stale handoff; True = hand off.

        Runs for every stale challenger of a device with a registered
        active-scan need that is not durably gone. First denial starts an
        episode: the trigger time is
        recorded and the scheduler runs an active window on both the owner
        and the challenger. Later denials hand off once the rescue's accept
        time (the window has been actively scanning long enough that a
        capture cannot be a delayed passive one) postdates the trigger and
        the challenger's advertisement postdates the accept time while the
        owner stayed silent (old.time only advances when the owner wins, so
        the owner being heard invalidates the episode), and re-trigger if
        no window materialized within the retry wait.
        """
        pending = self._rescue_triggered.get(new.address)
        if pending is None or old.time >= pending:
            # No episode, or the owner has been heard since the last
            # trigger: start a fresh episode.
            self._rescue_triggered[new.address] = new.time
            self._auto_scheduler.trigger_rescue(new.address, new.source, old.source)
            if self._debug:
                _LOGGER.debug(
                    "%s (%s): Deferring switch from %s to %s; triggered"
                    " rescue active scan (time elapsed:%s > stale"
                    " seconds:%s)",
                    new.name,
                    new.address,
                    self._async_describe_source(old),
                    self._async_describe_source(new),
                    elapsed,
                    stale_seconds,
                )
            return False
        accept_after = self._auto_scheduler._rescue_accept_after.get(new.address, 0.0)
        # The accept grace alone is empty for slow advertisers (a
        # 60s-interval sensor cannot re-advertise within it), so the
        # handoff additionally requires one full advertising interval
        # (stale_seconds minus the wobble) since the trigger; that
        # guarantees the owner one transmission opportunity to invalidate
        # the episode regardless of the device's cadence.
        if (
            accept_after >= pending
            and new.time > accept_after
            and new.time - pending > stale_seconds - TRACKER_BUFFERING_WOBBLE_SECONDS
        ):
            # A rescue window's accept time postdates the trigger and the
            # owner is still silent: hand off. The accept time is the max
            # across both sides, so a deaf owner whose window never ran
            # does not block the handoff; being deaf is exactly the case
            # being handed off from.
            del self._rescue_triggered[new.address]
            if self._debug:
                _LOGGER.debug(
                    "%s (%s): Switching from %s to %s (owner silent"
                    " after rescue active scan; accept time %s,"
                    " triggered at %s)",
                    new.name,
                    new.address,
                    self._async_describe_source(old),
                    self._async_describe_source(new),
                    accept_after,
                    pending,
                )
            return True
        if accept_after < pending and new.time - pending > _RESCUE_SCAN_RETRY_SECONDS:
            # The triggered window never materialized (scanner busy,
            # dispatch lost): restart the episode and try again. Advancing
            # the trigger time spaces retries by the retry interval instead
            # of re-triggering on every advertisement once it elapses. A
            # recorded accept means the episode is merely waiting out the
            # gates above; restarting would clobber it. Net effect of the
            # retry cadence: the rescue only accelerates handoffs for
            # advertisers faster than roughly the retry interval, and the
            # durably-gone backstop governs slower ones.
            self._rescue_triggered[new.address] = new.time
            self._auto_scheduler.trigger_rescue(new.address, new.source, old.source)
        return False

    def _record_demotion(self, address: str, new_source: str, old_source: str) -> None:
        """
        Move ownership in the reclaim-hysteresis set for an active RSSI switch.

        The new source becomes owner (leaves the demoted set) and the old owner
        joins it. ``old_source`` is guaranteed registered by the predicate that
        calls this. Runs only on an actual switch, so the set create is cheap.
        """
        demoted = self._demoted_sources.get(address)
        if demoted is None:
            demoted = set()
            self._demoted_sources[address] = demoted
        demoted.discard(new_source)
        demoted.add(old_source)

    def get_bluez_mgmt_ctl(self) -> MGMTBluetoothCtl | None:
        """
        Get the BlueZ management controller if available.

        Returns:
            The MGMTBluetoothCtl instance or None if not available

        """
        return self._mgmt_ctl

    def _handle_name_cache_miss(
        self,
        service_info: BluetoothServiceInfoBleak,
        cached_name: str | None,
    ) -> None:
        """
        Handle the cold path when cached_name is not service_info.name.

        Called from _scanner_adv_received only when the cached name and
        the incoming name are different str objects (steady-state
        identity match is filtered out at the call site). Walks through
        three cases:

        1. The incoming ad has no real name (empty or the MAC fallback
           set by base_scanner): patch service_info from the cache if we
           have one; this is the path that lets passive scanners inherit
           a name learned by an active scanner.
        2. No cached name yet: store the incoming name directly if it is
           real; no patch needed since the cache now matches.
        3. Cached and incoming are both real but differ: apply the
           prefix rule via _update_name_cache and patch service_info
           with whatever the cache settled on.
        """
        # When we patch service_info.name and service_info.device.name,
        # we also clear service_info._advertisement so the lazy rebuild
        # in BluetoothServiceInfoBleak._advertisement_internal picks up
        # the canonical name and propagates it to bleak callbacks via
        # advertisement.local_name. Remote scanners arrive with
        # _advertisement = None (see base_scanner.py:657), but
        # HaScanner.on_advertisement (scanner.py:331) pre-sets it to
        # bleak's AdvertisementData, so without this invalidation a
        # local passive scanner whose dispatched view we patch would
        # still hand bleak callbacks an AdvertisementData with the
        # original (missing) local_name.
        if (
            not service_info.name
            or service_info.name is service_info.address
            or service_info.name == service_info.address
        ):
            if cached_name is not None:
                service_info.name = cached_name
                service_info.device.name = cached_name
                service_info._advertisement = None
            return
        if cached_name is None:
            self._name_cache[service_info.address] = service_info.name
            return
        if cached_name == service_info.name:
            return
        self._update_name_cache(service_info.address, service_info.name)
        cached_name = self._name_cache[service_info.address]
        if cached_name is not service_info.name and cached_name != service_info.name:
            service_info.name = cached_name
            service_info.device.name = cached_name
            service_info._advertisement = None

    def seed_name_cache(self, address: str, name: str) -> None:
        """
        Apply the prefix rule to the cross-scanner name cache.

        Python-visible entry point intended for cold paths such as
        BaseHaScanner.restore_discovered_devices (called once per scanner
        at startup). The hot per-advertisement path does not use this
        method; it inlines the steady-state checks and calls the internal
        cdef _update_name_cache directly.
        """
        self._update_name_cache(address, name)

    def _update_name_cache(self, address: str, name: str) -> None:
        """
        Update the cross-scanner name cache for an address.

        Applies the case-folded prefix-extension rule:
        - identical name -> no-op (fastest path; identity check first)
        - empty name or name == address -> no-op (never pollute the cache
          with the address fallback used by base_scanner)
        - cached is None -> store new
        - new is a case-folded extension of cached -> store new
          (e.g. "Onv" -> "Onvis XXX")
        - cached is a case-folded extension of new -> keep cached
          (e.g. "Onvis XXX" -> "Onv" is a truncation)
        - neither is a case-folded prefix of the other -> rename, store new
          (e.g. "Onv" -> "Donkey")

        Performance note: after the steady-state identity / equality short
        circuits, length-based dispatch ensures we do at most ONE
        str.startswith per call (instead of up to two), since a prefix
        relationship is only possible when the shorter string could be a
        prefix of the longer. Compares casefolded lengths because casefold
        can change length for some characters (e.g. German "ß" -> "ss").
        """
        cached = self._name_cache.get(address)
        if cached is name:
            return
        if not name or name == address:
            return
        if cached is None:
            self._name_cache[address] = name
            return
        if cached == name:
            return
        cached_cf = cached.casefold()
        name_cf = name.casefold()
        cached_len = len(cached_cf)
        name_len = len(name_cf)
        if name_len > cached_len:
            # New is longer -> only "extension" or "rename" are possible.
            # Either way the new name wins (extension upgrades, rename replaces).
            self._name_cache[address] = name
            return
        if name_len < cached_len:
            # New is shorter -> "truncation" (keep cached) or "rename" (replace).
            if cached_cf.startswith(name_cf):
                return
            self._name_cache[address] = name
            return
        # Equal casefolded length, raw not equal -> case-only diff or rename.
        if cached_cf == name_cf:
            return
        self._name_cache[address] = name

    def scanner_adv_received(self, service_info: BluetoothServiceInfoBleak) -> None:
        """
        Handle a new advertisement from any scanner.

        Callbacks from all the scanners arrive here.

        This is the cpdef entry point for external callers.
        Internal callers should use _scanner_adv_received directly
        to avoid cpdef virtual dispatch overhead.
        """
        self._scanner_adv_received(service_info)

    def _scanner_adv_received(  # noqa: C901
        self, service_info: BluetoothServiceInfoBleak
    ) -> None:
        """
        Handle a new advertisement from any scanner (internal cdef path).

        Callbacks from all the scanners arrive here.
        """
        # Pre-filter noisy apple devices as they can account for 20-35% of the
        # traffic on a typical network.
        if (
            len(service_info.service_data) == 0
            and len(service_info.manufacturer_data) == 1
            and (apple_data := service_info.manufacturer_data.get(APPLE_MFR_ID))
        ):
            apple_cstr = apple_data
            if apple_cstr[0] not in {
                APPLE_IBEACON_START_BYTE,
                APPLE_HOMEKIT_START_BYTE,
                APPLE_HOMEKIT_NOTIFY_START_BYTE,
                APPLE_DEVICE_ID_START_BYTE,
                APPLE_FINDMY_START_BYTE,
            }:
                return

        # Cross-scanner name cache. Only the steady-state identity check
        # is inlined here because this code runs on every advertisement
        # after the Apple pre-filter; the rest is handled in a cdef
        # helper to keep this method readable. The hot path is a single
        # dict.get plus a pointer compare; the function call to the
        # helper only fires when the cached name and the incoming name
        # are different str objects, which excludes the dominant case of
        # the same scanner re-broadcasting the same name.
        cached_name = self._name_cache.get(service_info.address)
        if cached_name is not service_info.name:
            self._handle_name_cache_miss(service_info, cached_name)

        if service_info.connectable:
            old_connectable_service_info = self._connectable_history.get(
                service_info.address
            )
        else:
            old_connectable_service_info = None

        source = service_info.source
        old_service_info = self._all_history.get(service_info.address)
        # Maintain the smoothed RSSI used by cross-source owner arbitration
        # so a stationary device does not flap between similar-distance
        # proxies on momentary RSSI spikes. The bucket only exists once an
        # address is seen from more than one source; a single-proxy device
        # just takes the miss-lookup below and skips the rest (its
        # new_smoothed is never read, since the predicate bails when the
        # bucket is None). Runs before the same-payload short-circuit below
        # because RSSI changes even when the payload does not.
        #
        # A missing RSSI (proxies occasionally drop it) must NOT be folded in:
        # NO_RSSI_VALUE (-127) would poison the average and, because the EWMA
        # remembers it, drag the smoothed value down for several adverts
        # afterwards, manufacturing phantom weak readings that flap ownership.
        # So a no-RSSI advert keeps the last good smoothed value and writes
        # nothing; it never seeds or updates the average.
        new_smoothed = 0.0
        # Fast path for proxy-free setups: when no address has ever been seen
        # from more than one source the whole map is empty, so skip the keyed
        # lookup and only pay an O(1) emptiness check.
        smoothed_bucket = (
            self._smoothed_rssi.get(service_info.address)
            if self._smoothed_rssi
            else None
        )
        if smoothed_bucket is not None:
            rssi = service_info.rssi or NO_RSSI_VALUE
            prev_smoothed = smoothed_bucket.get(source)
            if not service_info.rssi:
                # No RSSI: keep the last good smoothed value (or NO_RSSI_VALUE
                # with no history, which simply loses arbitration) and do not
                # write, so the dropped reading can never poison the average.
                if prev_smoothed is not None:
                    new_smoothed = prev_smoothed
                else:
                    new_smoothed = NO_RSSI_VALUE
            elif prev_smoothed is not None:
                # prev_double is a cdef double, so the EWMA stays C-only.
                prev_double = prev_smoothed
                new_smoothed = (
                    _RSSI_SMOOTHING_FACTOR * rssi
                    + (1.0 - _RSSI_SMOOTHING_FACTOR) * prev_double
                )
                smoothed_bucket[source] = new_smoothed
            else:
                new_smoothed = rssi
                smoothed_bucket[source] = new_smoothed
        elif (
            old_service_info is not None
            and old_service_info.source is not source
            and old_service_info.source != source
            # Only seed against a source that is still registered. An old
            # source can linger in _all_history after it unregisters (the
            # unavailable sweep clears it later), and seeding its stale RSSI
            # would reintroduce the source the unregister cleanup just dropped,
            # defeating the "re-registered scanner starts fresh" guarantee.
            and old_service_info.source in self._sources
        ):
            # First cross-source sighting: seed the bucket from the existing
            # owner. Seed the new source only from a real reading; if it has no
            # RSSI, leave it unseeded so it loses arbitration via NO_RSSI_VALUE
            # (keeping the current owner) rather than priming the average with
            # -127 or stealing ownership without a signal. Its next real advert
            # seeds it cleanly.
            smoothed_bucket = {
                old_service_info.source: old_service_info.rssi or NO_RSSI_VALUE
            }
            if service_info.rssi:
                new_smoothed = service_info.rssi
                smoothed_bucket[source] = new_smoothed
            else:
                new_smoothed = NO_RSSI_VALUE
            self._smoothed_rssi[service_info.address] = smoothed_bucket

        # This logic is complex due to the many combinations of scanners
        # that are supported.
        #
        # We need to handle multiple connectable and non-connectable scanners
        # and we need to handle the case where a device is connectable on one scanner
        # but not on another.
        #
        # The device may also be connectable only by a scanner that has worse
        # signal strength than a non-connectable scanner.
        #
        # all_history - the history of all advertisements from all scanners with the
        #               best advertisement from each scanner
        # connectable_history - the history of all connectable advertisements from all
        #                       scanners with the best advertisement from each
        #                       connectable scanner
        #
        if old_service_info is not None and self._should_keep_previous_adv(
            old_service_info, service_info, smoothed_bucket, new_smoothed, True
        ):
            # If we are rejecting the new advertisement and the device is connectable
            # but not in the connectable history or the connectable source is the same
            # as the new source, we need to add it to the connectable history
            if service_info.connectable:
                if old_connectable_service_info is not None and (
                    # If it's the same as the preferred source, we're done; we know
                    # we prefer the old advertisement from the check above.
                    old_connectable_service_info is old_service_info
                    # Otherwise the old connectable came from a different source;
                    # re-run the predicate against the connectable history entry.
                    or self._should_keep_previous_adv(
                        old_connectable_service_info,
                        service_info,
                        smoothed_bucket,
                        new_smoothed,
                        False,
                    )
                ):
                    return

                self._connectable_history[service_info.address] = service_info

            return

        if service_info.connectable:
            self._connectable_history[service_info.address] = service_info

        self._all_history[service_info.address] = service_info

        # Hand the advertisement to the auto-scan scheduler right after
        # _all_history is updated. Ownership-flip detection (a different
        # scanner taking over a device's source) needs to fire even when
        # the advertisement payload is identical to the previous one;
        # the data-comparison short-circuit below would otherwise hide
        # that flip from the scheduler. Local-typed assignment so
        # cython.locals casts to AutoScanScheduler and the call is a
        # direct vtable dispatch even though _auto_scheduler is stored
        # untyped on BluetoothManager.
        auto_scheduler = self._auto_scheduler
        auto_scheduler.on_advertisement(service_info)

        # Track advertisement intervals to determine when we need to
        # switch adapters or mark a device as unavailable
        if (
            (
                last_source := self._advertisement_tracker.sources.get(
                    service_info.address
                )
            )
            is not None
            and last_source is not service_info.source
            and last_source != service_info.source
        ):
            # Source changed, remove the old address from the tracker
            self._advertisement_tracker.async_remove_address(service_info.address)
        if service_info.address not in self._advertisement_tracker.intervals:
            self._advertisement_tracker.async_collect(service_info)

        # If the advertisement data is the same as the last time we saw it, we
        # don't need to do anything else unless its connectable and we are missing
        # connectable history for the device so we can make it available again
        # after unavailable callbacks.
        if (
            # Ensure its not a connectable device missing from connectable history
            not (service_info.connectable and old_connectable_service_info is None)
            # Than check if advertisement data is the same
            and old_service_info is not None
            # This is a bit complex because we want to skip all the
            # PyObject_RichCompare overhead as its can be upwards of
            # 65% of the time spent in this method. The common case
            # is that its the same object for remote scanners.
            and not (
                (
                    service_info.manufacturer_data
                    is not old_service_info.manufacturer_data
                    and service_info.manufacturer_data
                    != old_service_info.manufacturer_data
                )
                or (
                    service_info.service_data is not old_service_info.service_data
                    and service_info.service_data != old_service_info.service_data
                )
                or (
                    service_info.service_uuids is not old_service_info.service_uuids
                    and service_info.service_uuids != old_service_info.service_uuids
                )
                or (
                    service_info.name is not old_service_info.name
                    and service_info.name != old_service_info.name
                )
            )
        ):
            return

        # A non-connectable scanner may currently be the closest path, but if a
        # still-registered connectable scanner also has a path to the device we
        # surface this advertisement as connectable so connectable callbacks and
        # discovery fire (the BleakClient routes any connection attempt to the
        # connectable path). connectable_history is only pruned by the periodic
        # unavailable check, so validate the stored entry's source is still
        # registered before trusting it as a live connectable path. This lookup is
        # deferred to here (after the identical-advertisement short-circuit above)
        # so the dominant non-connectable rebroadcast hot path never pays it.
        if (
            not service_info.connectable
            and (
                connectable_path := self._connectable_history.get(service_info.address)
            )
            is not None
            and connectable_path.source in self._sources
        ):
            service_info = service_info._as_connectable()

        if service_info.connectable and self._bleak_callbacks:
            # Bleak callbacks must get a connectable device
            advertisement_data = service_info._advertisement_internal()
            for bleak_callback in self._bleak_callbacks:
                _dispatch_bleak_callback(
                    bleak_callback, service_info.device, advertisement_data
                )

        self._subclass_discover_info(service_info)

    def async_clear_advertisement_history(self, address: str) -> None:
        """
        Clear cached advertisement history for a device.

        Causes the next advertisement from this address to be treated as new
        data, bypassing both the advertisement-merging logic in scanners and
        the change-detection guard. Intended for devices that encode state in
        mutually-exclusive service UUIDs.
        """
        self._all_history.pop(address, None)
        self._connectable_history.pop(address, None)
        self._name_cache.pop(address, None)
        self._smoothed_rssi.pop(address, None)
        self._demoted_sources.pop(address, None)
        self._rescue_triggered.pop(address, None)
        for scanner in self._sources.values():
            scanner._previous_service_info.pop(address, None)

    def _discover_service_info(self, service_info: BluetoothServiceInfoBleak) -> None:
        """
        Discover a new service info.

        This method is intended to be overridden by subclasses.
        """

    def _async_describe_source(self, service_info: BluetoothServiceInfoBleak) -> str:
        """Describe a source."""
        if scanner := self._sources.get(service_info.source):
            description = scanner.name
        else:
            description = service_info.source
        if service_info.connectable:
            description += " [connectable]"
        return description

    def _async_remove_unavailable_callback_internal(
        self,
        unavailable_callbacks: dict[
            str, set[Callable[[BluetoothServiceInfoBleak], None]]
        ],
        address: str,
        callbacks: set[Callable[[BluetoothServiceInfoBleak], None]],
        callback: Callable[[BluetoothServiceInfoBleak], None],
    ) -> None:
        """Remove a callback."""
        callbacks.remove(callback)
        if not callbacks:
            del unavailable_callbacks[address]

    def async_track_unavailable(
        self,
        callback: Callable[[BluetoothServiceInfoBleak], None],
        address: str,
        connectable: bool,
    ) -> Callable[[], None]:
        """Register a callback."""
        if connectable:
            unavailable_callbacks = self._connectable_unavailable_callbacks
        else:
            unavailable_callbacks = self._unavailable_callbacks
        callbacks = unavailable_callbacks.setdefault(address, set())
        callbacks.add(callback)
        return partial(
            self._async_remove_unavailable_callback_internal,
            unavailable_callbacks,
            address,
            callbacks,
            callback,
        )

    def async_ble_device_from_address(
        self, address: str, connectable: bool
    ) -> BLEDevice | None:
        """Return the BLEDevice if present."""
        histories = self._connectable_history if connectable else self._all_history
        if history := histories.get(address):
            return history.device
        return None

    def async_address_present(self, address: str, connectable: bool) -> bool:
        """Return if the address is present."""
        histories = self._connectable_history if connectable else self._all_history
        return address in histories

    def async_discovered_service_info(
        self, connectable: bool
    ) -> Iterable[BluetoothServiceInfoBleak]:
        """Return all the discovered services info."""
        histories = self._connectable_history if connectable else self._all_history
        return histories.values()

    def async_last_service_info(
        self, address: str, connectable: bool
    ) -> BluetoothServiceInfoBleak | None:
        """Return the last service info for an address."""
        histories = self._connectable_history if connectable else self._all_history
        return histories.get(address)

    def async_address_reachability_diagnostics(
        self, address: str, intent: BluetoothReachabilityIntent
    ) -> str:
        """
        Return a human-readable explanation of an address's reachability.

        Intended for embedding in error and log messages when a device cannot
        be found or used. The ``intent`` selects which facts are relevant: a
        caller that only consumes advertisements (``PASSIVE_ADVERTISEMENT`` /
        ``ACTIVE_ADVERTISEMENT``) does not care about connectable paths or
        connection slots, while a caller that wants to connect (``CONNECTION``)
        does. This is read-only and side-effect free, and is only meant for the
        cold error path, not the hot advertisement path.

        The returned string is for embedding in human-readable error and log
        messages only; its wording and format are not stable and must not be
        parsed. The address is not included, callers already have it in context.
        """
        now = monotonic_time_coarse()
        parts: list[str] = []
        # All scanners (connectable and non-connectable) that currently see the
        # address. Materialized once; reused for the per-scanner detail below.
        devices = self.async_scanner_devices_by_address(address, False)

        if intent is BluetoothReachabilityIntent.CONNECTION:
            self._append_connection_diagnostics(address, devices, parts)
        else:
            self._append_advertisement_diagnostics(address, devices, parts)

        parts.append(self._scanner_availability_summary())

        for device in devices:
            scanner = device.scanner
            detail = (
                f"{scanner.name} (connectable={scanner.connectable}, "
                f"rssi={device.advertisement.rssi}"
            )
            if intent is BluetoothReachabilityIntent.CONNECTION:
                detail += (
                    f", failures={scanner.connection_failures(address)}, "
                    f"in_progress={scanner.connections_in_progress()}"
                )
                if (allocations := scanner.get_allocations()) is not None:
                    detail += f", slots={allocations.free}/{allocations.slots}"
            parts.append(detail + ")")

        if (info := self._all_history.get(address)) is not None:
            if (via_scanner := self._sources.get(info.source)) is not None:
                via = via_scanner.name
            else:
                via = info.source
            parts.append(f"last advertisement {now - info.time:.0f}s ago via {via}")

        return "; ".join(parts)

    def _scanner_availability_summary(self) -> str:
        """
        Summarize how many scanners are registered, scanning and connectable.

        A scanner pauses scanning while it has a connection in progress, so a
        device can disappear from every scanner if they are all busy connecting;
        this is called out explicitly because no advertisements can be received
        while no scanner is scanning.
        """
        scanners = self.async_current_scanners()
        total = len(scanners)
        scanning = 0
        connecting = 0
        connectable = 0
        # A scanner pauses scanning while it has a connection in progress, so
        # in normal operation scanning and connecting_count are mutually
        # exclusive. Count them independently anyway so the "all paused
        # connecting" advice below stays correct even if that invariant drifts.
        for scanner in scanners:
            if scanner.connectable:
                connectable += 1
            if scanner.scanning:
                scanning += 1
            if scanner.connecting_count:
                connecting += 1
        summary = (
            f"{total} scanner(s) registered, {scanning} scanning, "
            f"{connectable} connectable"
        )
        if connecting:
            summary += f", {connecting} paused while connecting"
        if total and scanning == 0:
            summary += (
                "; no scanner is currently scanning so no advertisements can be "
                "received"
            )
            if connecting == total:
                summary += (
                    " (all are paused retrying connections; the available adapters "
                    "are overloaded, add more Bluetooth adapters or proxies)"
                )
        return summary

    def _append_connection_diagnostics(
        self,
        address: str,
        devices: list[BluetoothScannerDevice],
        parts: list[str],
    ) -> None:
        """Append connectable-path reachability facts for a connect intent."""
        if address in self._connectable_history:
            parts.append("in connectable history")
        elif address in self._all_history:
            parts.append("only in non-connectable history (no connectable path)")
        else:
            parts.append("unknown (never seen by any scanner)")

        # History outlives any scanner's discovered cache (scanner churn,
        # cache expiry, passive or_patterns miss); not an out of slots case.
        if not devices and address in self._all_history:
            parts.append("no scanner currently has it in its discovered devices")
            return

        connectable_devices = [d for d in devices if d.scanner.connectable]
        non_connectable_devices = [d for d in devices if not d.scanner.connectable]
        if not connectable_devices and non_connectable_devices:
            parts.append(
                f"seen by {len(non_connectable_devices)} scanner(s) but none with"
                " a connectable path"
            )
            return

        # Only consider scanners that actually report slot allocations; a
        # scanner returning None (e.g. a local adapter that does not track
        # slots) tells us nothing, so it must not suppress or trigger the
        # message. We only claim the reporting scanners are full, not that
        # every connectable path is exhausted.
        reported = [
            allocations
            for d in connectable_devices
            if (allocations := d.scanner.get_allocations()) is not None
            and allocations.slots > 0
        ]
        if reported and all(a.free == 0 for a in reported):
            parts.append(
                "connectable scanner(s) that report slot allocations are all full"
            )

    def _append_advertisement_diagnostics(
        self,
        address: str,
        devices: list[BluetoothScannerDevice],
        parts: list[str],
    ) -> None:
        """Append advertisement-only reachability facts for an advertisement intent."""
        # Advertisement callers only need adverts, so connectable paths and
        # connection slots are irrelevant; report only whether the device is
        # being seen and by how many scanners. ``_all_history`` outlives any
        # single scanner's discovered cache, so an address can be in history
        # while no scanner currently has it cached; do not claim it is still
        # advertising in that case.
        if devices:
            parts.append(f"advertising, seen by {len(devices)} scanner(s)")
        elif address in self._all_history:
            parts.append("previously seen but no scanner currently has it cached")
        else:
            parts.append("unknown (never seen by any scanner)")

    def _async_unregister_scanner_internal(
        self,
        scanners: set[BaseHaScanner],
        scanner: BaseHaScanner,
        connection_slots: int | None,
    ) -> None:
        """Unregister a scanner."""
        if scanner not in scanners:
            _LOGGER.debug("Scanner %s already unregistered; skipping", scanner.name)
            return
        _LOGGER.debug("Unregistering scanner %s", scanner.name)
        self._advertisement_tracker.async_remove_source(scanner.source)
        scanners.discard(scanner)
        scanner._clear_connection_history()
        self._sources.pop(scanner.source, None)
        self._warned_passive_active_scan.discard(scanner.source)
        # Drop this source's smoothed RSSI from every address bucket so a
        # re-registered scanner starts fresh and a stale value can't win, and
        # remove any bucket left empty so the map can return to truly empty
        # (re-enabling the proxy-free fast path) instead of lingering until the
        # unavailable sweep clears it.
        source = scanner.source
        emptied: list[str] = []
        for address, bucket in self._smoothed_rssi.items():
            bucket.pop(source, None)
            if not bucket:
                emptied.append(address)
        for address in emptied:
            del self._smoothed_rssi[address]
        # Drop this source from every reclaim-hysteresis set so a re-registered
        # scanner is not penalized for an ownership it lost before it went away,
        # and remove any set left empty.
        emptied_demoted: list[str] = []
        for address, demoted in self._demoted_sources.items():
            demoted.discard(source)
            if not demoted:
                emptied_demoted.append(address)
        for address in emptied_demoted:
            del self._demoted_sources[address]
        self._adapter_sources.pop(scanner.adapter, None)
        self._async_clear_allocations(source)
        if connection_slots:
            self.slot_manager.remove_adapter(scanner.adapter)
        if (idx := scanner.adapter_idx) is not None:
            self._side_channel_scanners.pop(idx, None)
        self._auto_scheduler.remove_scanner(scanner)
        self._async_on_scanner_registration(scanner, HaScannerRegistrationEvent.REMOVED)
        # Last so a failure to schedule cannot strand the teardown above.
        # BlueZ keeps a removed adapter's links up and the slot bookkeeping
        # just cleared has forgotten them.
        self._async_disconnect_clients(scanner)

    def _async_add_background_task(
        self, coro: Coroutine[Any, Any, None], name: str
    ) -> None:
        """Run a coroutine as a background task that is cancelled on stop."""
        if TYPE_CHECKING:
            assert self._loop is not None
        task = self._loop.create_task(coro, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._on_background_task_done)

    def _on_background_task_done(self, task: asyncio.Task[None]) -> None:
        """Drop a finished background task, logging an escaped exception."""
        self._background_tasks.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:
            _LOGGER.error("Background task %s failed", task.get_name(), exc_info=exc)

    def _async_disconnect_clients(self, scanner: BaseHaScanner) -> None:
        """Disconnect the clients still connected through a removed scanner."""
        if not scanner._clients:
            return
        if self.shutdown:
            # Abandoned to BlueZ/the kernel; scheduling after stop would
            # leak tasks past the loop's lifetime.
            _LOGGER.debug(
                "Shutdown; not disconnecting %d client(s) from removed scanner %s",
                len(scanner._clients),
                scanner.source,
            )
            return
        clients = list(scanner._clients)
        self._async_add_background_task(
            self._async_disconnect_all(clients, scanner),
            f"disconnect clients of {scanner.source}",
        )
        # After scheduling, so a failure to schedule keeps the entries.
        scanner._clients.clear()

    async def _async_disconnect_all(
        self, clients: list[HaBleakClientWrapper], scanner: BaseHaScanner
    ) -> None:
        """Disconnect clients concurrently."""
        try:
            # Each child logs its own failures; return_exceptions keeps one
            # misbehaving client from abandoning its siblings mid gather.
            results = await asyncio.gather(
                *(self._async_disconnect_client(client, scanner) for client in clients),
                return_exceptions=True,
            )
            for client, result in zip(clients, results, strict=True):
                if isinstance(result, BaseException):
                    # Only a BaseException can escape the child's handlers.
                    client._give_up()
                    _LOGGER.error(
                        "Unexpected error disconnecting client from removed scanner %s",
                        scanner.source,
                        exc_info=result,
                    )
        except asyncio.CancelledError:
            # Shutdown; the links are abandoned to BlueZ/the kernel.
            _LOGGER.debug(
                "Cancelled disconnecting %d client(s) from removed scanner %s",
                len(clients),
                scanner.source,
            )
            raise

    async def _async_disconnect_client(
        self, client: HaBleakClientWrapper, scanner: BaseHaScanner
    ) -> None:
        """Disconnect one client, logging failures instead of raising."""
        if client._connected_scanner is not scanner:
            # Reconnected through another scanner since this was scheduled.
            return
        device = client._connected_device
        address = device.address if device else "unknown"
        if not client.is_connected:
            _LOGGER.debug(
                "Client %s already down; not disconnecting from removed scanner %s",
                address,
                scanner.source,
            )
            client._untrack()
            return
        try:
            async with asyncio.timeout(CLIENT_DISCONNECT_TIMEOUT):
                await client.disconnect()
        except TimeoutError:
            # The expected shape for a proxy that went away; no traceback.
            client._give_up()
            _LOGGER.warning(
                "Timed out disconnecting client %s from removed scanner %s",
                address,
                scanner.source,
            )
        except Exception:  # pylint: disable=broad-except
            client._give_up()
            _LOGGER.exception(
                "Error disconnecting client %s from removed scanner %s",
                address,
                scanner.source,
            )

    def async_register_scanner(
        self,
        scanner: BaseHaScanner,
        connection_slots: int | None = None,
    ) -> CALLBACK_TYPE:
        """Register a new scanner."""
        _LOGGER.debug("Registering scanner %s", scanner.name)
        if (existing := self._sources.get(scanner.source)) is not None:
            # Always a caller bug: the source map is last writer wins and
            # the first unregister removes the live scanner, so the two
            # cannot coexist. Log loudly rather than raise so a scanner
            # setup path is not aborted in production for it.
            _LOGGER.error(
                "Scanner %s is being registered with source %s which is "
                "already registered by scanner %s; a source must be unique "
                "per registered scanner and the previous registration "
                "should be cancelled first",
                scanner.name,
                scanner.source,
                existing.name,
            )
        if scanner.connectable:
            scanners = self._connectable_scanners
        else:
            scanners = self._non_connectable_scanners
        # Seed zeroed allocations so the source is visible immediately,
        # unless an allocation push already arrived before registration.
        if scanner.source not in self._allocations:
            self._allocations[scanner.source] = _zeroed_allocations(scanner.source)
        scanners.add(scanner)
        scanner._clear_connection_history()
        self._sources[scanner.source] = scanner
        self._adapter_sources[scanner.adapter] = scanner.source
        if (idx := scanner.adapter_idx) is not None:
            self._side_channel_scanners[idx] = scanner
        if connection_slots:
            self.slot_manager.register_adapter(scanner.adapter, connection_slots)
            self.async_on_allocation_changed(
                self.slot_manager.get_allocations(scanner.adapter)
            )
        self._auto_scheduler.add_scanner(scanner)
        # Covers a scanner registered already in passive mode (no later
        # set_requested_mode to trigger scanner_mode_changed).
        self._async_warn_if_passive_with_active_scan(scanner)
        self._async_on_scanner_registration(scanner, HaScannerRegistrationEvent.ADDED)
        return partial(
            self._async_unregister_scanner_internal, scanners, scanner, connection_slots
        )

    def async_register_bleak_callback(
        self, callback: AdvertisementDataCallback, filters: dict[str, set[str]]
    ) -> CALLBACK_TYPE:
        """Register a callback."""
        callback_entry = BleakCallback(callback, filters)
        self._bleak_callbacks.add(callback_entry)
        # Replay the history since otherwise we miss devices
        # that were already discovered before the callback was registered
        # or we are in passive mode
        for history in self._connectable_history.values():
            _dispatch_bleak_callback(
                callback_entry, history.device, history.advertisement
            )

        return partial(self._bleak_callbacks.remove, callback_entry)

    def async_register_active_scan(
        self,
        address: str,
        scan_interval: float | None = None,
        scan_duration: float | None = None,
    ) -> CALLBACK_TYPE:
        """
        Declare an on-demand active-scan need for a specific address.

        Colon-form MAC addresses are normalized to upper-case to
        match BlueZ / ESPHome / Shelly source addresses; UUIDs (no
        colons, used by macOS CoreBluetooth) are passed through
        as-is since CoreBluetooth preserves case on its source
        addresses.

        ``scan_interval`` / ``scan_duration`` default to
        DEFAULT_ACTIVE_SCAN_INTERVAL (300s, 5 min) and
        DEFAULT_ACTIVE_SCAN_DURATION (10s); pass smaller values to
        get a tighter cadence. The effective window is clamped to
        [AUTO_WINDOW_MIN_DURATION, AUTO_WINDOW_MAX_DURATION]
        (5s..35s) and coalesced with other due requests for the
        scanner; very large ``scan_duration`` values are capped.
        ``scan_interval`` is measured between window starts (not
        between successive windows). ACTIVE / PASSIVE scanners
        ignore the request. Returns a cancel callable.
        """
        if not address:
            msg = "address must be a non-empty string"
            raise ValueError(msg)
        if scan_interval is None:
            scan_interval = DEFAULT_ACTIVE_SCAN_INTERVAL
        if scan_duration is None:
            scan_duration = DEFAULT_ACTIVE_SCAN_DURATION
        # Reject non-finite values explicitly: NaN compared to anything
        # returns False, so a NaN would slip past the lower-bound
        # checks below and end up in _due_at and call_later as a NaN
        # due-time / duration, busy-looping the worker.
        if not math.isfinite(scan_interval) or scan_interval < MIN_ACTIVE_SCAN_INTERVAL:
            msg = (
                f"scan_interval must be a finite number >= "
                f"{MIN_ACTIVE_SCAN_INTERVAL:.0f}s"
            )
            raise ValueError(msg)
        if not math.isfinite(scan_duration) or scan_duration < MIN_ACTIVE_SCAN_DURATION:
            msg = (
                f"scan_duration must be a finite number >= "
                f"{MIN_ACTIVE_SCAN_DURATION:.0f}s"
            )
            raise ValueError(msg)
        # MAC addresses (colon-form) get upper-cased to match BlueZ /
        # ESPHome conventions; UUIDs (macOS CoreBluetooth) pass
        # through as-is.
        normalized = address.upper() if ":" in address else address
        request = ActiveScanRequest(normalized, scan_interval, scan_duration)
        self._auto_scheduler.add_request(request)
        # An active scan now exists: warn about any passive-only scanner
        # that could own such a device and silently starve it.
        for scanner in self._sources.values():
            self._async_warn_if_passive_with_active_scan(scanner)
        return partial(self._auto_scheduler.remove_request, request)

    async def async_request_active_scan(self, duration: float | None = None) -> None:
        """
        Run an on-demand active sweep across every AUTO scanner.

        Intended for HA config-flow discovery: probes the bus
        actively without waiting for the 12 h rediscovery cadence,
        awaits ``duration`` so the caller can then read
        newly-discovered advertisements. Default 10s; clamped to
        ``[AUTO_WINDOW_MIN_DURATION, AUTO_WINDOW_MAX_DURATION]`` by
        the scheduler. Concurrent callers dedupe to one bus-wide
        window (a longer request extends the in-flight one); see
        ``AutoScanScheduler.async_request_active_scan``.
        """
        if duration is None:
            duration = DEFAULT_ON_DEMAND_SWEEP_DURATION
        if not math.isfinite(duration) or duration <= 0.0:
            msg = "duration must be a finite positive number"
            raise ValueError(msg)
        await self._auto_scheduler.async_request_active_scan(duration)

    def async_release_connection_slot(self, device: BLEDevice) -> None:
        """Release a connection slot."""
        self.slot_manager.release_slot(device)

    def async_allocate_connection_slot(self, device: BLEDevice) -> bool:
        """Allocate a connection slot."""
        return self.slot_manager.allocate_slot(device)

    def async_get_learned_advertising_interval(self, address: str) -> float | None:
        """Get the learned advertising interval for a MAC address."""
        return self._intervals.get(address)

    def async_get_fallback_availability_interval(self, address: str) -> float | None:
        """Get the fallback availability timeout for a MAC address."""
        return self._fallback_intervals.get(address)

    def async_set_fallback_availability_interval(
        self, address: str, interval: float
    ) -> None:
        """Override the fallback availability timeout for a MAC address."""
        self._fallback_intervals[address] = interval

    def _async_slot_manager_changed(self, event: AllocationChangeEvent) -> None:
        """Handle slot manager changes."""
        self.async_on_allocation_changed(
            self.slot_manager.get_allocations(event.adapter)
        )

    def _unregister_source_callback(
        self,
        callbacks_dict: dict[Any, set[Callable[..., None]]],
        source: object,
        callback: Callable[..., None],
    ) -> None:
        """Unregister a source-keyed callback."""
        if (callbacks := callbacks_dict.get(source)) is not None:
            callbacks.discard(callback)
            if not callbacks:
                del callbacks_dict[source]

    def _dispatch_source_callbacks(
        self,
        callbacks_dict: dict[Any, set[Callable[..., None]]],
        source: object,
        payload: object,
        label: str,
    ) -> None:
        """Dispatch payload to source-specific and global (None) callbacks."""
        for source_key in (source, None):
            if not (callbacks := callbacks_dict.get(source_key)):
                continue
            for callback_ in callbacks.copy():
                try:
                    callback_(payload)
                except Exception:  # pylint: disable=broad-except
                    _LOGGER.exception("Error in %s", label)

    def _async_clear_allocations(self, source: str) -> None:
        """
        Drop stored allocations for a source and notify subscribers.

        Dispatches a zeroed allocation so subscribers stop rendering the
        removed source's stale addresses. Tolerates a missing entry (two
        scanners sharing a source, the first unregister already cleared
        it) so teardown is never aborted midway; the zeroed dispatch is
        still sent so subscribers converge either way.
        """
        self._allocations.pop(source, None)
        self._dispatch_source_callbacks(
            self._allocations_callbacks,
            source,
            _zeroed_allocations(source),
            "allocation callback",
        )

    def async_on_allocation_changed(self, allocations: Allocations) -> None:
        """Call allocation callbacks."""
        source = self._adapter_sources.get(allocations.adapter, allocations.adapter)
        ha_slot_allocations = HaBluetoothSlotAllocations(
            source=source,
            slots=allocations.slots,
            free=allocations.free,
            allocated=allocations.allocated,
        )
        self._allocations[source] = ha_slot_allocations
        self._dispatch_source_callbacks(
            self._allocations_callbacks,
            source,
            ha_slot_allocations,
            "allocation callback",
        )

    def _async_on_scanner_registration(
        self, scanner: BaseHaScanner, event: HaScannerRegistrationEvent
    ) -> None:
        """Call scanner callbacks."""
        self._dispatch_source_callbacks(
            self._scanner_registration_callbacks,
            scanner.source,
            HaScannerRegistration(event, scanner),
            "scanner callback",
        )

    def async_current_allocations(
        self, source: str | None = None
    ) -> list[HaBluetoothSlotAllocations] | None:
        """
        Return the current allocations.

        An entry with ``slots=0`` means the source has not reported slot
        information (yet), not that its slots are exhausted; consumers
        should filter on ``slots > 0`` before reasoning about exhaustion.
        """
        if source:
            if allocations := self._allocations.get(source):
                return [allocations]
            return []
        return list(self._allocations.values())

    def async_register_allocation_callback(
        self,
        callback: Callable[[HaBluetoothSlotAllocations], None],
        source: str | None = None,
    ) -> CALLBACK_TYPE:
        """
        Register a callback to be called when an allocations change.

        When a source's scanner is unregistered, a zeroed
        ``HaBluetoothSlotAllocations`` (slots=0, free=0, allocated=[]) is
        dispatched so subscribers stop rendering its stale addresses;
        ``HaScannerRegistrationEvent.REMOVED`` on
        ``async_register_scanner_registration_callback`` disambiguates
        removal from an empty but present scanner. Registration seeds a
        zeroed entry without dispatching: it is visible immediately via
        ``async_current_allocations`` and the first callback fires when
        slot information is actually reported.
        """
        self._allocations_callbacks.setdefault(source, set()).add(callback)
        return partial(
            self._unregister_source_callback,
            self._allocations_callbacks,
            source,
            callback,
        )

    def async_register_scanner_registration_callback(
        self, callback: Callable[[HaScannerRegistration], None], source: str | None
    ) -> CALLBACK_TYPE:
        """Register a callback to be called when a scanner is added or removed."""
        self._scanner_registration_callbacks.setdefault(source, set()).add(callback)
        return partial(
            self._unregister_source_callback,
            self._scanner_registration_callbacks,
            source,
            callback,
        )

    def async_current_scanners(self) -> list[BaseHaScanner]:
        """Return the current scanners."""
        return list(self._sources.values())

    def async_register_scanner_mode_change_callback(
        self, callback: Callable[[HaScannerModeChange], None], source: str | None
    ) -> CALLBACK_TYPE:
        """Register a callback to be called when a scanner mode changes."""
        self._scanner_mode_change_callbacks.setdefault(source, set()).add(callback)
        return partial(
            self._unregister_source_callback,
            self._scanner_mode_change_callbacks,
            source,
            callback,
        )

    def scanner_mode_changed(self, scanner: BaseHaScanner) -> None:
        """Notify callbacks that a scanner's mode has changed."""
        self._async_warn_if_passive_with_active_scan(scanner)
        self._dispatch_source_callbacks(
            self._scanner_mode_change_callbacks,
            scanner.source,
            HaScannerModeChange(
                scanner=scanner,
                requested_mode=scanner.requested_mode,
                current_mode=scanner.current_mode,
            ),
            "scanner mode change callback",
        )

    def _async_warn_if_passive_with_active_scan(self, scanner: BaseHaScanner) -> None:
        """
        Warn once if ``scanner`` is passive-only while active scans are wanted.

        A passive-only scanner never runs an active window, so if it
        becomes the closest scanner for a device that only answers on
        its scan response (and an integration has asked for active
        scans on that device) the device's data may be missing. Deduped
        per source; the entry is dropped on unregister or when the
        scanner leaves passive mode so a later relapse warns again. It is
        intentionally not reset when active-scan requests drop to zero:
        the warning is per-source config advice ("this scanner is
        passive, fix its mode"), so one warning per source is enough and
        a later request for a different device need not re-warn.
        """
        source = scanner.source
        if scanner.requested_mode is not BluetoothScanningMode.PASSIVE:
            self._warned_passive_active_scan.discard(source)
            return
        if (
            source in self._warned_passive_active_scan
            or not self._auto_scheduler.has_active_requests
        ):
            return
        self._warned_passive_active_scan.add(source)
        _LOGGER.warning(
            "Scanner %s is in passive-only mode but active scans have been "
            "requested for one or more devices; if it becomes the closest "
            "scanner for such a device the device will not be actively "
            "scanned and its data may be incomplete or missing. Set this "
            "scanner to active or auto",
            scanner.name,
        )
