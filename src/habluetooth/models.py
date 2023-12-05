"""Models for bluetooth."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Final

from bleak import BaseBleakClient
from bluetooth_data_tools import monotonic_time_coarse

from .manager import BluetoothManager

MONOTONIC_TIME: Final = monotonic_time_coarse


class CentralBluetoothManager:
    """Central Bluetooth Manager."""

    manager: BluetoothManager | None = None


def get_manager() -> BluetoothManager:
    """Get the BluetoothManager."""
    if TYPE_CHECKING:
        assert CentralBluetoothManager.manager is not None
    return CentralBluetoothManager.manager


def set_manager(manager: BluetoothManager) -> None:
    """Set the BluetoothManager."""
    CentralBluetoothManager.manager = manager


@dataclass(slots=True)
class HaBluetoothConnector:
    """Data for how to connect a BLEDevice from a given scanner."""

    client: type[BaseBleakClient]
    source: str
    can_connect: Callable[[], bool]


class BluetoothScanningMode(Enum):
    """The mode of scanning for bluetooth devices."""

    PASSIVE = "passive"
    ACTIVE = "active"
