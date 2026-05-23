"""Tests for the cross-scanner name cache on BluetoothManager."""

import time
from datetime import timedelta
from unittest.mock import patch

import pytest
from freezegun import freeze_time

from habluetooth import HaBluetoothConnector, get_manager

from . import (
    InjectableRemoteScanner as _SeedFakeScanner,
)
from . import (
    MockBleakClient,
    generate_advertisement_data,
    generate_ble_device,
    inject_advertisement_with_source,
    utcnow,
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


def test_name_cache_equal_value_different_objects_noop() -> None:
    """Two str objects with identical value hit the cached == name no-op path."""
    manager = get_manager()
    address = "AA:BB:CC:DD:EE:0B"
    first = b"Onvis XXX".decode()
    second = b"Onvis XXX".decode()
    assert first is not second
    manager.seed_name_cache(address, first)
    manager.seed_name_cache(address, second)
    # Value unchanged; the original object is still cached (no rewrite).
    assert manager._name_cache[address] is first


def test_name_cache_equal_length_case_only_diff_keeps_cached() -> None:
    """Equal-length casefolded names differing only in case keep the cached value."""
    manager = get_manager()
    address = "AA:BB:CC:DD:EE:0C"
    manager.seed_name_cache(address, "Onv")
    manager.seed_name_cache(address, "ONV")
    assert manager._name_cache[address] == "Onv"


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
async def test_second_scanner_same_name_different_object_noop() -> None:
    """
    Second scanner reporting the same name (different str object) is a no-op.

    Exercises the cached_name == service_info.name early-return inside
    _handle_name_cache_miss: identity mismatch but value match.
    """
    manager = get_manager()
    connector = HaBluetoothConnector(
        MockBleakClient, "mock_bleak_client", lambda: False
    )

    scanner_a = _SeedFakeScanner("esp32-a", "esp32-a", connector, True)
    scanner_b = _SeedFakeScanner("esp32-b", "esp32-b", connector, True)
    cancels = [
        scanner_a.async_setup(),
        manager.async_register_scanner(scanner_a),
        scanner_b.async_setup(),
        manager.async_register_scanner(scanner_b),
    ]

    address = "44:44:33:11:23:56"
    # Use bytes.decode() so each scanner gets a distinct str object.
    device_a = generate_ble_device(address, b"Onvis XXX".decode(), {}, rssi=-60)
    adv_a = generate_advertisement_data(local_name=b"Onvis XXX".decode(), rssi=-60)
    scanner_a.inject_advertisement(device_a, adv_a)
    cached_first = manager._name_cache[address]

    device_b = generate_ble_device(address, b"Onvis XXX".decode(), {}, rssi=-50)
    adv_b = generate_advertisement_data(
        local_name=b"Onvis XXX".decode(),
        manufacturer_data={1: b"\x01"},
        rssi=-50,
    )
    scanner_b.inject_advertisement(device_b, adv_b)

    # Cache value unchanged; original object is preserved (no rewrite).
    assert manager._name_cache[address] is cached_first

    for c in cancels:
        c()


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_local_passive_scanner_advertisement_rebuilt_with_cached_name() -> None:
    """
    Local passive scanner's AdvertisementData is rebuilt with the cached name.

    HaScanner.on_advertisement (scanner.py) pre-sets service_info._advertisement
    to bleak's AdvertisementData, so without invalidation the patched
    service_info.name would not propagate to advertisement.local_name for
    bleak callbacks. Mimics that producer shape via inject_advertisement_with_source
    (which also pre-sets _advertisement) and verifies the dispatch path
    rebuilds the AdvertisementData with the patched name.
    """
    manager = get_manager()
    address = "44:44:33:11:23:58"

    # Seed the cache from an active scanner.
    active_device = generate_ble_device(address, "Onvis XXX", {}, rssi=-60)
    active_adv = generate_advertisement_data(local_name="Onvis XXX", rssi=-60)
    inject_advertisement_with_source(active_device, active_adv, "active-source")
    assert manager._name_cache[address] == "Onvis XXX"

    # Local passive scanner sees the same device without a name; its
    # AdvertisementData has local_name = None and is pre-built on
    # service_info._advertisement (matches HaScanner.on_advertisement).
    passive_device = generate_ble_device(address, None, {}, rssi=-30)
    passive_adv = generate_advertisement_data(
        local_name=None,
        manufacturer_data={123: b"\xde\xad"},
        rssi=-30,
    )
    inject_advertisement_with_source(passive_device, passive_adv, "passive-source")

    patched = manager._all_history[address]
    assert patched.name == "Onvis XXX"
    assert patched.device.name == "Onvis XXX"
    # _advertisement was invalidated on patch, so the lazy rebuild picks
    # up the canonical name; bleak callbacks now see it.
    assert patched.advertisement.local_name == "Onvis XXX"


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_truncation_from_second_scanner_patched_back() -> None:
    """
    Scanner reporting a truncated name has its service_info patched back.

    Exercises the post-update patch branch in _handle_name_cache_miss:
    _update_name_cache decides to keep the longer cached value, then the
    incoming service_info is patched back to that cached value so the
    dispatch carries the canonical name.
    """
    manager = get_manager()
    connector = HaBluetoothConnector(
        MockBleakClient, "mock_bleak_client", lambda: False
    )

    active = _SeedFakeScanner("esp32-full", "esp32-full", connector, True)
    secondary = _SeedFakeScanner("esp32-trunc", "esp32-trunc", connector, True)
    cancels = [
        active.async_setup(),
        manager.async_register_scanner(active),
        secondary.async_setup(),
        manager.async_register_scanner(secondary),
    ]

    address = "44:44:33:11:23:57"
    active_device = generate_ble_device(address, "Onvis XXX", {}, rssi=-60)
    active_adv = generate_advertisement_data(local_name="Onvis XXX", rssi=-60)
    active.inject_advertisement(active_device, active_adv)
    assert manager._name_cache[address] == "Onvis XXX"

    # Secondary scanner sees only the truncated name (e.g. no SCAN_RSP yet).
    trunc_device = generate_ble_device(address, "Onv", {}, rssi=-50)
    trunc_adv = generate_advertisement_data(
        local_name="Onv", manufacturer_data={1: b"\x01"}, rssi=-50
    )
    secondary.inject_advertisement(trunc_device, trunc_adv)

    # Cache keeps the longer name and the dispatched view is patched back.
    assert manager._name_cache[address] == "Onvis XXX"
    assert manager._all_history[address].name == "Onvis XXX"
    assert manager._all_history[address].device.name == "Onvis XXX"

    for c in cancels:
        c()


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
