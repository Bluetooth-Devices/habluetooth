"""Central manager for bluetooth."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .manager import BluetoothManager


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
