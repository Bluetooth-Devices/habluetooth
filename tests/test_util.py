"""Tests for habluetooth.util."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from habluetooth.util import async_reset_adapter, is_docker_env


@pytest.mark.asyncio
async def test_async_reset_adapter_with_hci_adapter() -> None:
    """An hciN adapter delegates to bluetooth_auto_recovery.recover_adapter."""
    mock_recover = AsyncMock(return_value=True)
    with patch("habluetooth.util.recover_adapter", mock_recover):
        result = await async_reset_adapter("hci3", "AA:BB:CC:DD:EE:FF", True)
    assert result is True
    mock_recover.assert_awaited_once_with(3, "AA:BB:CC:DD:EE:FF", True)


@pytest.mark.asyncio
async def test_async_reset_adapter_returns_false_when_adapter_is_none() -> None:
    """No adapter → recover_adapter is not invoked, returns False."""
    mock_recover = AsyncMock()
    with patch("habluetooth.util.recover_adapter", mock_recover):
        result = await async_reset_adapter(None, "AA:BB:CC:DD:EE:FF", False)
    assert result is False
    mock_recover.assert_not_called()


@pytest.mark.asyncio
async def test_async_reset_adapter_returns_false_for_non_hci_adapter() -> None:
    """A non-hci adapter (e.g. CoreBluetooth) returns False without recovery."""
    mock_recover = AsyncMock()
    with patch("habluetooth.util.recover_adapter", mock_recover):
        result = await async_reset_adapter("Core Bluetooth", "AA:BB:CC:DD:EE:FF", False)
    assert result is False
    mock_recover.assert_not_called()


def test_is_docker_env_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """/.dockerenv present → True."""
    is_docker_env.cache_clear()
    monkeypatch.setattr("habluetooth.util.Path.exists", lambda self: True)
    assert is_docker_env() is True
    is_docker_env.cache_clear()


def test_is_docker_env_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """/.dockerenv missing → False."""
    is_docker_env.cache_clear()
    monkeypatch.setattr("habluetooth.util.Path.exists", lambda self: False)
    assert is_docker_env() is False
    is_docker_env.cache_clear()
