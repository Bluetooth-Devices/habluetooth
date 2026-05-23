"""Constants."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from enum import Enum
from typing import Final

CALLBACK_TYPE = Callable[[], None]

SOURCE_LOCAL: Final = "local"

START_TIMEOUT = 15
STOP_TIMEOUT = 5

# The maximum time between advertisements for a device to be considered
# stale when the advertisement tracker cannot determine the interval.
#
# We have to set this quite high as we don't know
# when devices fall out of the ESPHome device (and other non-local scanners)'s
# stack like we do with BlueZ so its safer to assume its available
# since if it does go out of range and it is in range
# of another device the timeout is much shorter and it will
# switch over to using that adapter anyways.
#
FALLBACK_MAXIMUM_STALE_ADVERTISEMENT_SECONDS: Final = 60 * 15

# The maximum time between advertisements for a device to be considered
# stale when the advertisement tracker can determine the interval for
# connectable devices.
#
# BlueZ uses 180 seconds by default but we give it a bit more time
# to account for the esp32's bluetooth stack being a bit slower
# than BlueZ's.
CONNECTABLE_FALLBACK_MAXIMUM_STALE_ADVERTISEMENT_SECONDS: Final = 195


# We must recover before we hit the 180s mark
# where the device is removed from the stack
# or the devices will go unavailable. Since
# we only check every 30s, we need this number
# to be
# 180s Time when device is removed from stack
# - 30s check interval
# - 30s scanner restart time * 2
#
SCANNER_WATCHDOG_TIMEOUT: Final = 90
# How often to check if the scanner has reached
# the SCANNER_WATCHDOG_TIMEOUT without seeing anything
SCANNER_WATCHDOG_INTERVAL: Final = timedelta(seconds=30)


UNAVAILABLE_TRACK_SECONDS: Final = 60 * 5


# AUTO scanning mode: each scanner gets its first sweep
# AUTO_INITIAL_SWEEP_DELAY after joining, then every
# AUTO_REDISCOVERY_INTERVAL, serialized across scanners.
AUTO_INITIAL_SWEEP_DELAY: Final = 60 * 4
AUTO_REDISCOVERY_INTERVAL: Final = 60 * 60 * 12
AUTO_REDISCOVERY_SWEEP_DURATION: Final = 15.0

# Per-callback scan_duration is clamped into this range. The floor
# matches the validation in async_register_active_scan; the ceiling is
# the longest single ACTIVE flip we'll ever do for one device tick.
AUTO_WINDOW_MIN_DURATION: Final = 5.0
AUTO_WINDOW_MAX_DURATION: Final = 30.0

# When the worker ticks, also pull in per-device entries due within
# this many seconds so devices registered at staggered times coalesce
# into one window instead of triggering back-to-back active flips
# seconds apart. Devices pulled forward are scanned slightly early
# (at most AUTO_COALESCE_LOOKAHEAD seconds) on this tick; their
# next_due is advanced from now, so they sync up with the rest of
# the bucket on subsequent ticks.
AUTO_COALESCE_LOOKAHEAD: Final = 15.0

# Minimum values accepted by async_register_active_scan. Anything
# shorter would just churn the radio without giving the device time to
# respond on its scan response.
MIN_ACTIVE_SCAN_INTERVAL: Final = 60.0
MIN_ACTIVE_SCAN_DURATION: Final = 5.0

# Defaults used by async_register_active_scan when the caller does
# not specify a cadence. One 10s active window every 5 minutes per
# device covers the typical temperature/humidity/battery sensor case
# without burning the proxy's radio or the sensor's battery; an
# integration that genuinely needs faster updates can pass a smaller
# scan_interval explicitly.
DEFAULT_ACTIVE_SCAN_INTERVAL: Final = 300.0
DEFAULT_ACTIVE_SCAN_DURATION: Final = 10.0

# Default duration for an on-demand sweep triggered by
# BluetoothManager.async_request_active_scan (HA config-flow discovery).
# 10s gives every device on the bus a chance to advertise during the
# window without holding the caller too long.
DEFAULT_ON_DEMAND_SWEEP_DURATION: Final = 10.0


FAILED_ADAPTER_MAC = "00:00:00:00:00:00"


ADV_RSSI_SWITCH_THRESHOLD: Final = 16
# The switch threshold for the rssi value
# to switch to a different adapter for advertisements
# Note that this does not affect the connection
# selection that uses RSSI_SWITCH_THRESHOLD from
# bleak_retry_connector


# Connection parameter constants (units of 1.25ms for intervals)
# Fast connection parameters for initial connection and service discovery
FAST_MIN_CONN_INTERVAL: Final = 0x06  # 6 * 1.25ms = 7.5ms (BLE minimum)
FAST_MAX_CONN_INTERVAL: Final = 0x06  # 6 * 1.25ms = 7.5ms
FAST_CONN_LATENCY: Final = 0  # No latency for fast response
FAST_CONN_TIMEOUT: Final = 1000  # 1000 * 10ms = 10s

# Medium connection parameters for standard operation
# Balanced for stability with WiFi-based BLE proxies
MEDIUM_MIN_CONN_INTERVAL: Final = 0x07  # 7 * 1.25ms = 8.75ms
MEDIUM_MAX_CONN_INTERVAL: Final = 0x09  # 9 * 1.25ms = 11.25ms
MEDIUM_CONN_LATENCY: Final = 0  # No latency
MEDIUM_CONN_TIMEOUT: Final = 800  # 800 * 10ms = 8s

# Bluetooth address types
BDADDR_BREDR: Final = 0x00
BDADDR_LE_PUBLIC: Final = 0x01
BDADDR_LE_RANDOM: Final = 0x02


class ConnectParams(Enum):
    """Connection parameter presets."""

    FAST = "fast"
    MEDIUM = "medium"
