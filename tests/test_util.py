"""Tests for habluetooth.util."""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import pytest

from habluetooth.util import async_reset_adapter, is_docker_env


@pytest.fixture
def mock_recover_adapter() -> Iterator[AsyncMock]:
    """Patch habluetooth.util.recover_adapter with an AsyncMock."""
    mock = AsyncMock()
    with patch("habluetooth.util.recover_adapter", mock):
        yield mock


@pytest.mark.asyncio
async def test_async_reset_adapter_with_hci_adapter(
    mock_recover_adapter: AsyncMock,
) -> None:
    """An hciN adapter delegates to bluetooth_auto_recovery.recover_adapter."""
    mock_recover_adapter.return_value = True
    assert await async_reset_adapter("hci3", "AA:BB:CC:DD:EE:FF", True) is True
    mock_recover_adapter.assert_awaited_once_with(3, "AA:BB:CC:DD:EE:FF", True)


@pytest.mark.asyncio
async def test_async_reset_adapter_returns_false_when_adapter_is_none(
    mock_recover_adapter: AsyncMock,
) -> None:
    """No adapter → recover_adapter is not invoked, returns False."""
    assert await async_reset_adapter(None, "AA:BB:CC:DD:EE:FF", False) is False
    mock_recover_adapter.assert_not_called()


@pytest.mark.asyncio
async def test_async_reset_adapter_returns_false_for_non_hci_adapter(
    mock_recover_adapter: AsyncMock,
) -> None:
    """A non-hci adapter (e.g. CoreBluetooth) returns False without recovery."""
    assert (
        await async_reset_adapter("Core Bluetooth", "AA:BB:CC:DD:EE:FF", False)
        is False
    )
    mock_recover_adapter.assert_not_called()


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
