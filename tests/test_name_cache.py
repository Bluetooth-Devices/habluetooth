"""Tests for the cross-scanner name cache on BluetoothManager."""

import time

import pytest
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bluetooth_data_tools import monotonic_time_coarse

from habluetooth import BaseHaRemoteScanner, HaBluetoothConnector, get_manager

from . import (
    MockBleakClient,
    generate_advertisement_data,
    generate_ble_device,
    inject_advertisement_with_source,
)


class _SeedFakeScanner(BaseHaRemoteScanner):
    """Minimal remote scanner that exposes inject_advertisement for tests."""

    def inject_advertisement(
        self,
        device: BLEDevice,
        advertisement_data: AdvertisementData,
        now: float | None = None,
    ) -> None:
        """Inject an advertisement through the scanner's normal entry point."""
        self._async_on_advertisement(
            device.address,
            advertisement_data.rssi,
            device.name,
            advertisement_data.service_uuids,
            advertisement_data.service_data,
            advertisement_data.manufacturer_data,
            advertisement_data.tx_power,
            {"scanner_specific_data": "test"},
            now or monotonic_time_coarse(),
        )


# ---------------------------------------------------------------------------
# Unit tests for the prefix-extension policy
# ---------------------------------------------------------------------------


def test_name_cache_empty_to_name() -> None:
    """First non-empty name observed is stored."""
    manager = get_manager()
    address = "AA:BB:CC:DD:EE:01"
    manager.seed_name_cache(address, "Onv")
    assert manager._name_cache[address] == "Onv"


def test_name_cache_extension_replaces_truncation() -> None:
    """A new name that extends the cached short name replaces it."""
    manager = get_manager()
    address = "AA:BB:CC:DD:EE:02"
    manager.seed_name_cache(address, "Onv")
    manager.seed_name_cache(address, "Onvis XXX")
    assert manager._name_cache[address] == "Onvis XXX"


def test_name_cache_truncation_keeps_cached() -> None:
    """A new name that is a truncation of the cached complete name is rejected."""
    manager = get_manager()
    address = "AA:BB:CC:DD:EE:03"
    manager.seed_name_cache(address, "Onvis XXX")
    manager.seed_name_cache(address, "Onv")
    assert manager._name_cache[address] == "Onvis XXX"


def test_name_cache_rename_replaces() -> None:
    """A completely different name (not prefix-related) replaces the cached name."""
    manager = get_manager()
    address = "AA:BB:CC:DD:EE:04"
    manager.seed_name_cache(address, "Onv")
    manager.seed_name_cache(address, "Donkey")
    assert manager._name_cache[address] == "Donkey"


def test_name_cache_same_name_noop() -> None:
    """Re-broadcasting the same name does not allocate a new cache entry."""
    manager = get_manager()
    address = "AA:BB:CC:DD:EE:05"
    manager.seed_name_cache(address, "Onv")
    cached_first = manager._name_cache[address]
    manager.seed_name_cache(address, "Onv")
    # Same string compares equal; identity may or may not match depending on
    # interning but the value must be unchanged.
    assert manager._name_cache[address] == cached_first


def test_name_cache_empty_name_noop() -> None:
    """An empty name never overwrites the cached value."""
    manager = get_manager()
    address = "AA:BB:CC:DD:EE:06"
    manager.seed_name_cache(address, "Onv")
    manager.seed_name_cache(address, "")
    assert manager._name_cache[address] == "Onv"


def test_name_cache_address_fallback_not_stored() -> None:
    """
    Address fallback (name == address) must not pollute the cache.

    base_scanner sets info.name = address when no local_name is present.
    """
    manager = get_manager()
    address = "AA:BB:CC:DD:EE:07"
    manager.seed_name_cache(address, address)
    assert address not in manager._name_cache


def test_name_cache_address_fallback_does_not_overwrite() -> None:
    """The address-fallback no-op must not replace an existing cached name."""
    manager = get_manager()
    address = "AA:BB:CC:DD:EE:08"
    manager.seed_name_cache(address, "Onv")
    manager.seed_name_cache(address, address)
    assert manager._name_cache[address] == "Onv"


def test_name_cache_case_folded_extension() -> None:
    """The extension rule is case-folded: 'onv' is a prefix of 'ONVIS XXX'."""
    manager = get_manager()
    address = "AA:BB:CC:DD:EE:09"
    manager.seed_name_cache(address, "onv")
    manager.seed_name_cache(address, "ONVIS XXX")
    assert manager._name_cache[address] == "ONVIS XXX"


def test_name_cache_case_folded_truncation_keeps_cached() -> None:
    """Case-folded truncation: 'ONV' is a truncation of 'Onvis XXX'."""
    manager = get_manager()
    address = "AA:BB:CC:DD:EE:0A"
    manager.seed_name_cache(address, "Onvis XXX")
    manager.seed_name_cache(address, "ONV")
    assert manager._name_cache[address] == "Onvis XXX"


# ---------------------------------------------------------------------------
# Cross-scanner integration: passive scanner gains name from active scanner
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_passive_scanner_gains_name_from_active_scanner() -> None:
    """
    Passive scanner inherits the name learned by an active scanner.

    Uses real BaseHaRemoteScanner injection so the lazy AdvertisementData
    construction path in models._advertisement_internal is exercised.
    """
    manager = get_manager()
    connector = HaBluetoothConnector(
        MockBleakClient, "mock_bleak_client", lambda: False
    )

    active_scanner = _SeedFakeScanner("esp32-active", "esp32-active", connector, True)
    active_unsetup = active_scanner.async_setup()
    active_cancel = manager.async_register_scanner(active_scanner)

    passive_scanner = _SeedFakeScanner(
        "esp32-passive", "esp32-passive", connector, True
    )
    passive_unsetup = passive_scanner.async_setup()
    passive_cancel = manager.async_register_scanner(passive_scanner)

    address = "44:44:33:11:23:50"

    # Active scanner reports the device with its full SCAN_RSP-derived name.
    active_device = generate_ble_device(address, "Onvis XXX", {}, rssi=-60)
    active_adv = generate_advertisement_data(local_name="Onvis XXX", rssi=-60)
    active_scanner.inject_advertisement(active_device, active_adv)

    assert manager._name_cache[address] == "Onvis XXX"
    assert manager._all_history[address].name == "Onvis XXX"

    # Passive scanner reports the same address without a name, with stronger
    # RSSI so it wins the source-preference comparison.
    passive_device = generate_ble_device(address, None, {}, rssi=-30)
    passive_adv = generate_advertisement_data(
        local_name=None,
        # Distinct manufacturer data to force past the fast-path early-return
        # so we actually exercise the patch path.
        manufacturer_data={99: b"\x99"},
        rssi=-30,
    )
    passive_scanner.inject_advertisement(passive_device, passive_adv)

    # The patched view in _all_history must carry the active scanner's name.
    patched = manager._all_history[address]
    assert patched.name == "Onvis XXX"
    assert patched.device.name == "Onvis XXX"
    # And the AdvertisementData built on-demand for bleak callbacks must
    # carry it too.
    assert patched.advertisement.local_name == "Onvis XXX"

    active_cancel()
    active_unsetup()
    passive_cancel()
    passive_unsetup()


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_active_scanner_extension_propagates_across_sources() -> None:
    """
    An active scanner's longer name upgrades the cache populated by a passive one.

    Order: passive sees the short name first, then active sees the full
    SCAN_RSP-derived name. The cache must upgrade and the dispatched view
    must carry the longer name.
    """
    manager = get_manager()
    address = "44:44:33:11:23:51"

    # Passive scanner sees the shortened name first.
    passive_device = generate_ble_device(address, "Onv", {}, rssi=-80)
    passive_adv = generate_advertisement_data(local_name="Onv", rssi=-80)
    inject_advertisement_with_source(passive_device, passive_adv, "passive-source")
    assert manager._name_cache[address] == "Onv"

    # Active scanner sees the full SCAN_RSP-derived name.
    active_device = generate_ble_device(address, "Onvis XXX", {}, rssi=-70)
    active_adv = generate_advertisement_data(
        local_name="Onvis XXX",
        manufacturer_data={1: b"\x01"},
        rssi=-70,
    )
    inject_advertisement_with_source(active_device, active_adv, "active-source")

    assert manager._name_cache[address] == "Onvis XXX"
    assert manager._all_history[address].name == "Onvis XXX"


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_rename_replaces_across_sources() -> None:
    """A genuine rename (not a prefix relationship) replaces the cached name."""
    manager = get_manager()
    address = "44:44:33:11:23:52"

    device_a = generate_ble_device(address, "Onv", {}, rssi=-60)
    adv_a = generate_advertisement_data(local_name="Onv", rssi=-60)
    inject_advertisement_with_source(device_a, adv_a, "source-a")
    assert manager._name_cache[address] == "Onv"

    device_b = generate_ble_device(address, "Donkey", {}, rssi=-60)
    adv_b = generate_advertisement_data(
        local_name="Donkey",
        manufacturer_data={1: b"\x01"},
        rssi=-60,
    )
    inject_advertisement_with_source(device_b, adv_b, "source-b")
    assert manager._name_cache[address] == "Donkey"
    assert manager._all_history[address].name == "Donkey"


# ---------------------------------------------------------------------------
# Eviction
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_async_clear_advertisement_history_evicts_cache() -> None:
    """async_clear_advertisement_history removes the name cache entry."""
    manager = get_manager()
    address = "44:44:33:11:23:53"

    device = generate_ble_device(address, "Onvis XXX", {}, rssi=-60)
    adv = generate_advertisement_data(local_name="Onvis XXX", rssi=-60)
    inject_advertisement_with_source(device, adv, "source")
    assert address in manager._name_cache

    manager.async_clear_advertisement_history(address)
    assert address not in manager._name_cache


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_disappearance_evicts_cache() -> None:
    """A device that disappears via _async_check_unavailable evicts its cache entry."""
    from datetime import timedelta
    from unittest.mock import patch

    from freezegun import freeze_time

    from . import utcnow

    manager = get_manager()
    address = "44:44:33:11:23:54"

    device = generate_ble_device(address, "Onvis XXX", {}, rssi=-60)
    adv = generate_advertisement_data(local_name="Onvis XXX", rssi=-60)
    inject_advertisement_with_source(device, adv, "source")
    assert address in manager._name_cache

    future_time = utcnow() + timedelta(seconds=3600)
    future_monotonic_time = time.monotonic() + 3600
    with (
        freeze_time(future_time),
        patch(
            "habluetooth.manager.monotonic_time_coarse",
            return_value=future_monotonic_time,
        ),
    ):
        manager._async_check_unavailable()

    assert address not in manager._name_cache


# ---------------------------------------------------------------------------
# Seed on load from per-scanner persisted state
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_restore_discovered_devices_seeds_cache() -> None:
    """
    Restoring a scanner's persisted history seeds the shared name cache.

    Subsequent passive-scanner ads on the same address inherit the
    previously-known name without waiting for an active scanner to
    re-observe it.
    """
    manager = get_manager()
    connector = HaBluetoothConnector(
        MockBleakClient, "mock_bleak_client", lambda: False
    )

    src_scanner = _SeedFakeScanner("esp32-src", "esp32-src", connector, True)
    src_unsetup = src_scanner.async_setup()
    src_cancel = manager.async_register_scanner(src_scanner)

    address = "44:44:33:11:23:55"
    device = generate_ble_device(address, "Onvis XXX", {}, rssi=-60)
    adv = generate_advertisement_data(local_name="Onvis XXX", rssi=-60)
    src_scanner.inject_advertisement(device, adv)
    history = src_scanner.serialize_discovered_devices()

    # Fresh cache state; restore should re-populate it.
    manager._name_cache.pop(address, None)
    assert address not in manager._name_cache

    dst_scanner = _SeedFakeScanner("esp32-dst", "esp32-dst", connector, True)
    dst_unsetup = dst_scanner.async_setup()
    dst_cancel = manager.async_register_scanner(dst_scanner)
    dst_scanner.restore_discovered_devices(history)

    assert manager._name_cache[address] == "Onvis XXX"

    src_cancel()
    src_unsetup()
    dst_cancel()
    dst_unsetup()
