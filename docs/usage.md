(usage)=

# Usage

Assuming that you've followed the {ref}`installations steps <installation>`, you're now ready to use this package.

Start by importing it:

```python
import habluetooth
```

TODO: Document usage

## Pinning a device to a scanner

By default the connection path is chosen dynamically from RSSI, connection
failures and free slots. When a device has a dedicated proxy next to it,
several proxies that all hear the device can compete for it. Pin the address
to the source of the scanner that should own it:

```python
manager = habluetooth.get_manager()
manager.async_set_pinned_source("AA:BB:CC:DD:EE:FF", scanner.source)
manager.async_get_pinned_source("AA:BB:CC:DD:EE:FF")  # -> scanner.source
manager.async_set_pinned_source("AA:BB:CC:DD:EE:FF", None)  # remove the pin
```

The pin is a preference, not an exclusion: the pinned scanner is tried first
whenever it currently sees the device, and the normal scored order is used as
a fallback when it does not, or when it has no free connection slot.
