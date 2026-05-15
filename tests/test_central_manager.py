"""Tests for habluetooth.central_manager."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from habluetooth.central_manager import (
    CentralBluetoothManager,
    get_manager,
    set_manager,
)


@pytest.fixture
def preserve_manager() -> Iterator[None]:
    """Save and restore the CentralBluetoothManager singleton around a test."""
    original = CentralBluetoothManager.manager
    try:
        yield
    finally:
        CentralBluetoothManager.manager = original


def test_get_manager_raises_when_unset(preserve_manager: None) -> None:
    """get_manager() raises RuntimeError when no manager has been set."""
    CentralBluetoothManager.manager = None
    with pytest.raises(RuntimeError, match="BluetoothManager has not been set"):
        get_manager()


def test_set_manager_replaces_singleton(preserve_manager: None) -> None:
    """set_manager() stores the instance on the central holder."""
    sentinel = object()
    set_manager(sentinel)  # type: ignore[arg-type]
    assert CentralBluetoothManager.manager is sentinel
    assert get_manager() is sentinel
