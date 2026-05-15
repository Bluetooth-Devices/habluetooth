"""Tests for habluetooth.central_manager."""

from __future__ import annotations

import pytest

from habluetooth.central_manager import (
    CentralBluetoothManager,
    get_manager,
    set_manager,
)


def test_get_manager_raises_when_unset() -> None:
    """get_manager() raises RuntimeError when no manager has been set."""
    original = CentralBluetoothManager.manager
    CentralBluetoothManager.manager = None
    try:
        with pytest.raises(RuntimeError, match="BluetoothManager has not been set"):
            get_manager()
    finally:
        CentralBluetoothManager.manager = original


def test_set_manager_replaces_singleton() -> None:
    """set_manager() stores the instance on the central holder."""
    original = CentralBluetoothManager.manager
    sentinel = object()
    try:
        set_manager(sentinel)  # type: ignore[arg-type]
        assert CentralBluetoothManager.manager is sentinel
        assert get_manager() is sentinel
    finally:
        CentralBluetoothManager.manager = original
