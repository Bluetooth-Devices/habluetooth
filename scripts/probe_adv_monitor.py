"""
Probe BlueZ AdvertisementMonitor1 or_patterns behaviour on a live adapter.

Standalone: needs only ``dbus_fast`` and a BlueZ >= 5.56 started with
``--experimental`` on a kernel >= 5.10. Run it on the Bluetooth host:

    python scripts/probe_adv_monitor.py --adapter hci0 --duration 20

It registers advertisement monitors the same way bleak does (RegisterMonitor
first, export the object second) and records which devices each monitor
reports through DeviceFound, then prints a table answering:

1. which FLAGS byte values the devices in range actually advertise;
2. whether a single monitor silently drops patterns past the kernel limit
   (HCI_MAX_ADV_MONITOR_NUM_PATTERNS, 16 on mainline);
3. whether two monitors of 16 patterns each cover everything;
4. how much the legacy three value list (0x02, 0x06, 0x1a) misses.

It only registers monitors; it never starts discovery or connects.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from typing import Any, no_type_check

from dbus_fast import BusType, Message, MessageType, PropertyAccess
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, dbus_property, method

BLUEZ = "org.bluez"
MONITOR_IFACE = "org.bluez.AdvertisementMonitor1"
MANAGER_IFACE = "org.bluez.AdvertisementMonitorManager1"
DEVICE_IFACE = "org.bluez.Device1"
FLAGS_AD_TYPE = 0x01
ALL_FLAGS = list(range(0x20))
LEGACY_FLAGS = [0x02, 0x06, 0x1A]


def flags_pattern(value: int) -> list[Any]:
    """Return an or_pattern matching a FLAGS AD structure equal to value."""
    return [0, FLAGS_AD_TYPE, bytes([value])]


class Monitor(ServiceInterface):
    """One org.bluez.AdvertisementMonitor1 object recording DeviceFound."""

    def __init__(self, label: str, values: list[int]) -> None:
        super().__init__(MONITOR_IFACE)
        self.label = label
        self.values = values
        self.found: dict[str, float] = {}
        self.lost: dict[str, float] = {}
        self.activated = False
        self.released = False

    @method()
    @no_type_check
    def Release(self):  # noqa: N802
        self.released = True

    @method()
    @no_type_check
    def Activate(self):  # noqa: N802
        self.activated = True

    @method()
    @no_type_check
    def DeviceFound(self, device: o):  # noqa: F821, N802
        self.found.setdefault(device, time.monotonic())

    @method()
    @no_type_check
    def DeviceLost(self, device: o):  # noqa: F821, N802
        self.lost[device] = time.monotonic()

    @dbus_property(PropertyAccess.READ)
    @no_type_check
    def Type(self) -> s:  # noqa: F821, N802
        return "or_patterns"

    @dbus_property(PropertyAccess.READ)
    @no_type_check
    def Patterns(self) -> a(yyay):  # noqa: F821, N802
        return [flags_pattern(v) for v in self.values]


class Probe:
    """Register and tear down monitors on one adapter."""

    def __init__(self, bus: MessageBus, adapter: str) -> None:
        self.bus = bus
        self.adapter_path = f"/org/bluez/{adapter}"
        self._counter = 0

    async def call(
        self,
        path: str,
        iface: str,
        member: str,
        sig: str = "",
        body: list[Any] | None = None,
    ) -> Message:
        reply = await self.bus.call(
            Message(
                destination=BLUEZ,
                path=path,
                interface=iface,
                member=member,
                signature=sig,
                body=body or [],
            )
        )
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(
                f"{member} on {path} failed: {reply.error_name}: {reply.body}"
            )
        return reply

    async def manager_props(self) -> dict[str, Any]:
        reply = await self.call(
            self.adapter_path,
            "org.freedesktop.DBus.Properties",
            "GetAll",
            "s",
            [MANAGER_IFACE],
        )
        return {k: v.value for k, v in reply.body[0].items()}

    async def register(self, monitors: list[Monitor]) -> list[str]:
        paths: list[str] = []
        for mon in monitors:
            self._counter += 1
            path = f"/org/habluetooth/probe/{self._counter}"
            await self.call(
                self.adapter_path, MANAGER_IFACE, "RegisterMonitor", "o", [path]
            )
            # bleak exports after registering; BlueZ ignores the monitor otherwise.
            self.bus.export(path, mon)
            paths.append(path)
        return paths

    async def unregister(self, monitors: list[Monitor], paths: list[str]) -> None:
        for mon, path in zip(monitors, paths, strict=True):
            self.bus.unexport(path, mon)
            try:
                await self.call(
                    self.adapter_path, MANAGER_IFACE, "UnregisterMonitor", "o", [path]
                )
            except RuntimeError as err:
                print(f"  warning: {err}", file=sys.stderr)

    async def run_phase(
        self, name: str, monitors: list[Monitor], duration: float
    ) -> None:
        print(f"\n== {name}: {len(monitors)} monitor(s), {duration:.0f}s")
        paths = await self.register(monitors)
        try:
            await asyncio.sleep(duration)
        finally:
            await self.unregister(monitors, paths)
        for mon in monitors:
            state = "activated" if mon.activated else "NOT activated"
            if mon.released:
                state += ", released by BlueZ"
            print(f"  {mon.label}: {len(mon.found)} device(s), {state}")

    async def device_names(self) -> dict[str, str]:
        reply = await self.call(
            "/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects"
        )
        names: dict[str, str] = {}
        for path, ifaces in reply.body[0].items():
            if (dev := ifaces.get(DEVICE_IFACE)) is None:
                continue
            addr = dev["Address"].value
            name = dev["Name"].value if "Name" in dev else ""
            names[path] = f"{addr} {name}".strip()
        return names


def fmt_values(values: list[int]) -> str:
    return ",".join(f"{v:#04x}" for v in values)


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--adapter", default="hci0")
    parser.add_argument(
        "--duration", type=float, default=20.0, help="seconds per phase"
    )
    parser.add_argument(
        "--skip-census",
        action="store_true",
        help="skip the per value census (4 phases)",
    )
    args = parser.parse_args()

    try:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    except Exception as err:
        print(f"cannot connect to the system bus: {err}", file=sys.stderr)
        return 2
    probe = Probe(bus, args.adapter)

    try:
        props = await probe.manager_props()
    except RuntimeError as err:
        print(
            f"{MANAGER_IFACE} not available on {probe.adapter_path}: {err}\n"
            "BlueZ >= 5.56 started with --experimental and kernel >= 5.10 are required",
            file=sys.stderr,
        )
        return 2
    print(f"adapter {probe.adapter_path}")
    for key, value in props.items():
        print(f"  {key}: {value}")

    census: dict[int, set[str]] = {v: set() for v in ALL_FLAGS}
    if not args.skip_census:
        for start in range(0, len(ALL_FLAGS), 8):
            batch = ALL_FLAGS[start : start + 8]
            monitors = [Monitor(f"flags {v:#04x}", [v]) for v in batch]
            await probe.run_phase(
                f"census {fmt_values(batch)}", monitors, args.duration
            )
            for mon in monitors:
                census[mon.values[0]].update(mon.found)

    legacy = Monitor("legacy 0x02,0x06,0x1a", LEGACY_FLAGS)
    await probe.run_phase("legacy list", [legacy], args.duration)

    # Put the common values last so a 16 pattern cap would drop them.
    ordered = [v for v in ALL_FLAGS if v not in LEGACY_FLAGS] + LEGACY_FLAGS
    single = Monitor("32 patterns in one monitor", ordered)
    await probe.run_phase("truncation test", [single], args.duration)

    pair = [
        Monitor("monitor A (16 patterns)", ordered[:16]),
        Monitor("monitor B (16 patterns)", ordered[16:]),
    ]
    await probe.run_phase("two monitor test", pair, args.duration)
    two = set(pair[0].found) | set(pair[1].found)

    names = await probe.device_names()
    all_devices = (
        set().union(*census.values()) | set(legacy.found) | set(single.found) | two
    )

    print(
        "\n== per FLAGS value (census: devices seen by a monitor with only that value)"
    )
    print(f"{'flags':>6} {'census':>6} {'legacy':>6} {'32in1':>6} {'2x16':>6}  devices")
    for value in ALL_FLAGS:
        devs = census[value]
        if not devs and args.skip_census:
            continue
        in_legacy = len(devs & set(legacy.found))
        in_single = len(devs & set(single.found))
        in_two = len(devs & two)
        listing = "; ".join(names.get(p, p) for p in sorted(devs))
        print(
            f"{value:#06x} {len(devs):>6} {in_legacy:>6} {in_single:>6} {in_two:>6}  {listing}"
        )

    print("\n== totals")
    print(f"  distinct devices in any phase: {len(all_devices)}")
    print(f"  legacy list:        {len(legacy.found)}")
    print(f"  32 in one monitor:  {len(single.found)}")
    print(f"  two monitors of 16: {len(two)}")
    only_two = two - set(single.found)
    if only_two:
        print(
            f"  found by two monitors but not by the single 32 pattern monitor ({len(only_two)}):"
        )
        for path in sorted(only_two):
            print(f"    {names.get(path, path)}")
        print(
            "  => the single monitor is truncated; a second monitor is needed for full coverage"
        )
    missed = all_devices - set(legacy.found)
    if missed:
        print(f"  missed by the legacy list ({len(missed)}):")
        for path in sorted(missed):
            print(f"    {names.get(path, path)}")

    bus.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
