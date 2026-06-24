# Wiring the DBus-free local scanner into Home Assistant (experimental)

> **Experimental.** The management/L2CAP path (`HaScannerMgmt`, `HaMgmtClient`,
> `create_local_scanner`) is a work in progress; the API may change or be
> renamed. Nothing here is on by default — Home Assistant still constructs the
> bleak-backed `HaScanner` until this wiring lands.

The DBus-free path drives a local adapter entirely over the BlueZ management
socket and a raw L2CAP ATT channel (discovery, connect, pairing/bonding),
bypassing bluetoothd/DBus. `habluetooth` exposes everything Home Assistant needs
to opt in; the only change is in `homeassistant/components/bluetooth`.

## 1. Select the scanner via the factory

Today the bluetooth component constructs the scanner directly
(`homeassistant/components/bluetooth/__init__.py`):

```python
scanner = HaScanner(mode, adapter, address)
```

Swap that for the factory. It returns `HaScannerMgmt` when the management path is
usable, and falls back to the bleak-backed `HaScanner` otherwise, so it is safe
everywhere:

```python
from habluetooth import create_local_scanner

scanner = create_local_scanner(mode, adapter, address)
```

The returned object is a `BaseHaScanner`, so the rest of the flow is unchanged:
`scanner.async_setup()`, `async_register_scanner(hass, scanner, connection_slots=slots)`,
then `await scanner.async_start()`. The advertisement side-channel, connection
slots, and registration all work as before.

### When the factory picks the mgmt scanner

`create_local_scanner` returns `HaScannerMgmt` only when **all** hold; otherwise
it returns `HaScanner`:

- the host is Linux,
- the management socket is available and reports discovery capability
  (`CAP_NET_ADMIN`/`CAP_NET_RAW`),
- the adapter is an `hciN` adapter,
- a real adapter BD_ADDR is known (not `DEFAULT_ADDRESS`),
- the mode is not `AUTO` (the mgmt scanner does not yet implement
  active-window promotion, so `AUTO` stays on the bleak path).

Note Home Assistant already converts `AUTO` to `ACTIVE` for adapters that cannot
do passive scanning before this call, so on those adapters the mgmt scanner is
eligible.

### Recommended gating

Because the path is experimental, gate the swap behind an opt-in (a config/YAML
flag or an environment check) so it is off by default and a user can enable it
per install. Fall back to the plain `HaScanner` constructor when the flag is
off.

### Controller ownership (required before enabling the connect path)

The LE long-term-key table belongs to the **controller and its kernel**, not to
a process or container. `AF_BLUETOOTH`/HCI control sockets are not
network-namespaced, so a `habluetooth` running in the Home Assistant container
and a `bluetoothd` running on the host share the **same** `hciN` and the **same**
kernel bond table. The typical deployment (HA in a container, host `bluetoothd`)
is therefore a single shared controller, not two isolated ones.

The mgmt **discovery** side already coexists with host `bluetoothd` (it only
reads advertisement events; that is the existing side channel). The mgmt
**connect/pair** path does not: `LOAD_LONG_TERM_KEYS`, mgmt pairing, and the
L2CAP ACL/GATT are stateful on the shared controller and collide with a
`bluetoothd` that is still managing the same `hciN`. In particular
`LOAD_LONG_TERM_KEYS` replaces the controller's whole key list, clearing the
bonds `bluetoothd` loaded; those keys cannot be recovered (DBus never exposes
key material, and `bluetoothd`'s `/var/lib/bluetooth` store is not reachable from
the HA container).

So the model is **per-adapter partitioning**, never co-management of one `hciN`:

- Controllers `bluetoothd` manages stay on the bleak/DBus path (today's
  behavior); their bonds remain `bluetoothd`'s and are untouched.
- The mgmt connect path may own a controller only if `bluetoothd` is **not**
  managing it (for example a dedicated adapter the host bluetooth service is
  configured to ignore). It then creates its own bonds from the first pairing,
  so `LOAD_LONG_TERM_KEYS` only ever replaces its own list.

> **Factory gap.** `create_local_scanner` currently gates only on mgmt discovery
> capability and L2CAP availability; it has **no** signal for "this controller is
> not also managed by `bluetoothd`." Until that exclusivity gate exists, treat
> the connect path as safe only on a controller you know `bluetoothd` does not
> manage, and keep the opt-in above scoped to such adapters.

## 2. Persist bonds across restarts

Pairing over mgmt yields long-term keys that `habluetooth` keeps **in memory**
only. To survive a restart without re-pairing, the component persists them with
its own `Store` and seeds them back on startup. The scanner exposes the hooks;
see [Bond key persistence](bond_persistence.md) for the full worked example. In
short, for each scanner that is an `HaScannerMgmt`:

```python
from habluetooth import HaScannerMgmt

if isinstance(scanner, HaScannerMgmt):
    bond_storage.async_attach(scanner)  # restore_long_term_keys + change callback
```

Attach **before** `async_start()` so restored keys are loaded before the first
connection, and clear the change callback (`set_long_term_keys_changed_callback(None)`)
when the scanner is torn down.

## What does not change

- Scanner registration, connection-slot accounting, and the unavailable-tracking
  timer are inherited from `BaseHaScanner` and behave as before.
- Remote (ESPHome) scanners and non-Linux/macOS local adapters are untouched;
  the factory returns the existing `HaScanner` for them.
- No public API is removed; `HaScanner` remains available for the fallback path.
