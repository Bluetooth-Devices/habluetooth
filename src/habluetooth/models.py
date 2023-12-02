"""Models for bluetooth."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from bleak import BaseBleakClient
from bluetooth_data_tools import monotonic_time_coarse

MONOTONIC_TIME: Final = monotonic_time_coarse


@dataclass(slots=True)
class HaBluetoothConnector:
    """Data for how to connect a BLEDevice from a given scanner."""

    client: type[BaseBleakClient]
    source: str
    can_connect: Callable[[], bool]
