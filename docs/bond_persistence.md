# Bond key persistence (experimental)

> **Experimental.** This covers the DBus-free management/L2CAP path
> (`HaScannerMgmt` + `HaMgmtClient`). The API is not stable and may change or be
> renamed. Home Assistant does not select this path yet.

When pairing over the BlueZ management socket, the kernel hands back a
long-term key (LTK) per bond. `habluetooth` keeps these **in memory only** (it is
a library and does no disk I/O), so without help they are lost on restart and
the peer must be re-paired. To survive a restart, the consumer (Home Assistant)
persists them and seeds them back on startup.

Keys are **per adapter**: an LTK is tied to the local adapter identity it paired
with, so it is only valid on that adapter. Persist keyed by adapter.

## What `habluetooth` provides

- `LongTermKey` — the bonded key (`habluetooth.LongTermKey`).
- `long_term_key_to_dict()` / `long_term_key_from_dict()` — JSON-safe
  (de)serialization (the `bytes` fields become hex strings), shaped like the
  discovery-cache helpers.
- On `HaScannerMgmt`:
  - `export_long_term_keys() -> list[LongTermKey]` — snapshot for saving.
  - `restore_long_term_keys(keys)` — seed the store on startup.
  - `set_long_term_keys_changed_callback(callback)` — `callback` fires whenever
    the store changes (a pair captured a key, or an unpair removed one), so the
    consumer can schedule a debounced save.

## Wiring it in Home Assistant

Mirror the existing remote-scanner storage (`Store` + `async_delay_save`), but
keyed by adapter and holding a list of serialized keys:

```python
from habluetooth import (
    HaScannerMgmt,
    LongTermKeyDict,
    long_term_key_from_dict,
    long_term_key_to_dict,
)
from homeassistant.helpers.storage import Store

BOND_STORAGE_VERSION = 1
BOND_STORAGE_KEY = "bluetooth.bonds"
SAVE_DELAY = 5

StoredBonds = dict[str, list[LongTermKeyDict]]  # keyed by adapter (e.g. "hci0")


class BondStorage:
    def __init__(self, hass):
        self._store: Store[StoredBonds] = Store(
            hass, BOND_STORAGE_VERSION, BOND_STORAGE_KEY
        )
        self._data: StoredBonds = {}

    async def async_setup(self):
        self._data = await self._store.async_load() or {}

    def async_attach(self, scanner: HaScannerMgmt):
        adapter = scanner.adapter
        # Seed the scanner with any keys we saved last run.
        if saved := self._data.get(adapter):
            scanner.restore_long_term_keys(
                long_term_key_from_dict(key) for key in saved
            )
        # Save (debounced) whenever the scanner's bonds change.
        scanner.set_long_term_keys_changed_callback(
            lambda: self._async_save(scanner)
        )

    def _async_save(self, scanner: HaScannerMgmt):
        self._data[scanner.adapter] = [
            long_term_key_to_dict(key) for key in scanner.export_long_term_keys()
        ]
        self._store.async_delay_save(lambda: self._data, SAVE_DELAY)
```

Call `async_attach()` when the scanner is created and `set_long_term_keys_changed_callback(None)` if it is torn down.
