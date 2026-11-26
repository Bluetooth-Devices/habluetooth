(usage)=

# Usage

Assuming that you've followed the {ref}`installations steps <installation>`, you're now ready to use this package.

Start by importing it:

```python
import habluetooth
```

`habluetooth` is the Bluetooth core library used by Home Assistant. It is **not**
a daemon — it runs in-process inside an asyncio event loop and coordinates
multiple Bluetooth scanners (local USB/UART adapters via BlueZ, ESPHome remote
scanners, etc.) behind a single orchestrator. Most callers interact with it
through two layers:

- the **`BluetoothManager`** singleton, which tracks every advertisement seen
  across all scanners and answers "what have we discovered?" queries, and
- the **wrappers** (`HaBleakScannerWrapper` / `HaBleakClientWrapper`), which are
  drop-in, [`bleak`](https://github.com/hbldh/bleak)-compatible replacements for
  `BleakScanner` / `BleakClient` that route through the manager instead of
  opening their own radios.

The full public surface is exported from `habluetooth.__all__`; the API reference
is rendered at <https://habluetooth.readthedocs.io>.

## The manager

There is exactly one `BluetoothManager` per process. It is held in a module-level
singleton and accessed with `get_manager()` / `set_manager()`:

```python
from habluetooth import BluetoothManager, get_manager, set_manager

manager = BluetoothManager()
set_manager(manager)
await manager.async_setup()
```

`async_setup()` records the manager as the central singleton (if one is not
already set), binds it to the running event loop, refreshes the adapter list, and
starts the auto-scan scheduler. `get_manager()` returns the active manager and
raises `RuntimeError` if `async_setup()` (or `set_manager()`) has not run yet, so
the wrappers and helpers below all assume a manager is in place.

Shut down with:

```python
manager.async_stop()
```

`async_stop()` is synchronous: it tears down tracking, stops the scheduler, and
flips the manager into a shutdown state.

The constructor accepts optional `bluetooth_adapters` and `slot_manager`
dependencies; when omitted, sensible defaults are created. Home Assistant wires
these up for you — standalone callers normally only need the two-line setup
above.

## Scanning for devices

`HaBleakScannerWrapper` mirrors `bleak.BleakScanner` but shares the single
manager instance instead of starting a new radio scan. Register a detection
callback and start it like a normal Bleak scanner:

```python
from habluetooth import HaBleakScannerWrapper

def on_advertisement(device, advertisement_data):
    print(device.address, advertisement_data.rssi, advertisement_data.local_name)

scanner = HaBleakScannerWrapper(detection_callback=on_advertisement)
await scanner.start()
# ... receive callbacks ...
await scanner.stop()
```

It also works as an async context manager:

```python
async with HaBleakScannerWrapper(detection_callback=on_advertisement):
    ...
```

Only UUID service filters are honoured (`service_uuids=[...]` or the Bleak
`filters={"UUIDs": [...]}` form); other Bleak filter kinds are ignored with a
warning. The classmethods `find_device_by_address`, `find_device_by_name`,
`find_device_by_filter`, and `discover` are implemented against the manager's
existing discovery history rather than triggering a fresh scan.

## Querying discovered devices

If you don't need a Bleak-shaped scanner, read directly from the manager. Each
query takes a `connectable` flag selecting the connectable-only history or the
full (all-scanner) history:

```python
manager = get_manager()

# Latest advertisement for one address (or None)
info = manager.async_last_service_info("AA:BB:CC:DD:EE:FF", connectable=True)

# Is the address currently present?
present = manager.async_address_present("AA:BB:CC:DD:EE:FF", connectable=False)

# Resolve a BLEDevice you can hand to a client
ble_device = manager.async_ble_device_from_address("AA:BB:CC:DD:EE:FF", connectable=True)

# Iterate everything discovered so far
for service_info in manager.async_discovered_service_info(connectable=False):
    print(service_info.address, service_info.name, service_info.rssi)
```

The objects returned by these queries are `BluetoothServiceInfoBleak` instances,
which carry the parsed advertisement plus the underlying `BLEDevice` and
`AdvertisementData`.

To be notified when a device stops advertising, register an unavailable
callback with `async_track_unavailable(callback, address, connectable)`; it
returns a cancel callable.

## Connecting to a device

`HaBleakClientWrapper` is a drop-in `bleak.BleakClient` that connects through the
best available scanner the manager knows about, instead of letting Bleak spin up
its own scanner to resolve the address:

```python
from habluetooth import HaBleakClientWrapper

client = HaBleakClientWrapper(ble_device)  # or an address string
await client.connect()
try:
    if client.is_connected:
        ...  # read/write GATT as with a normal BleakClient
finally:
    await client.disconnect()
```

Passing a `BLEDevice` (resolved via `async_ble_device_from_address`) is preferred
over a bare address — it lets the manager pick the right backend without a
redundant lookup.

## On-demand active scanning

Scanners running in `BluetoothScanningMode.AUTO` default to passive scanning and
flip to active on demand. There are two ways to ask for active scans:

```python
# Per-address recurring active-scan need; returns a cancel callable.
cancel = manager.async_register_active_scan(
    "AA:BB:CC:DD:EE:FF",
    scan_interval=300.0,   # seconds between window starts (default 300)
    scan_duration=10.0,    # seconds per active window (default 10)
)
# ... later ...
cancel()

# One-off active sweep across every AUTO scanner (e.g. a discovery flow).
await manager.async_request_active_scan(duration=10.0)
```

`async_register_active_scan` validates its arguments (finite, above the
configured minimums) and normalises colon-form MAC addresses to upper case.
The effective window duration is clamped to `[AUTO_WINDOW_MIN_DURATION,
AUTO_WINDOW_MAX_DURATION]` and coalesced with other due requests on the same
scanner; `ACTIVE` and `PASSIVE` scanners ignore per-address requests.
`async_request_active_scan` awaits its `duration`, and concurrent callers dedupe
into a single bus-wide window (a longer request extends the in-flight one).

## Persisting the discovery cache

The discovery cache (per-scanner advertisement data) can be serialised to a
plain dict for storage and reconstructed on the next start. Use the provided
entry points rather than hand-rolling the format:

```python
from habluetooth import (
    discovered_device_advertisement_data_to_dict,
    discovered_device_advertisement_data_from_dict,
)

blob = discovered_device_advertisement_data_to_dict(discovered)
# ... persist `blob` (it is a JSON-serialisable TypedDict) ...
restored = discovered_device_advertisement_data_from_dict(blob)
```

`expire_stale_scanner_discovered_device_advertisement_data` prunes entries older
than the configured staleness thresholds before re-seeding a scanner. The cache
is intentionally rebuildable: a malformed blob is dropped and repopulated from
live scanning within seconds, so there is no on-disk migration to manage.
