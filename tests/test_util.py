"""Tests for habluetooth.util."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from habluetooth.util import (
    async_reset_adapter,
    coalesce_concurrent_future,
    is_docker_env,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


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
        await async_reset_adapter("Core Bluetooth", "AA:BB:CC:DD:EE:FF", False) is False
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


class _Coalesced:
    """Test fixture instance for the coalesce_concurrent_future decorator."""

    def __init__(self) -> None:
        self._fut: asyncio.Future[int] | None = None
        self.call_count = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.return_value = 42
        self.raise_exc: BaseException | None = None

    @coalesce_concurrent_future("_fut")
    async def work(self) -> int:
        self.call_count += 1
        self.started.set()
        await self.release.wait()
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.return_value


@pytest.mark.asyncio
async def test_coalesce_concurrent_future_single_call_returns_result() -> None:
    """A lone caller runs the wrapped coroutine and gets its result."""
    obj = _Coalesced()
    obj.release.set()
    assert await obj.work() == 42
    assert obj.call_count == 1
    assert obj._fut is None


@pytest.mark.asyncio
async def test_coalesce_concurrent_future_concurrent_callers_share_one_call() -> None:
    """Concurrent callers share a single underlying invocation."""
    obj = _Coalesced()
    leader = asyncio.create_task(obj.work())
    await obj.started.wait()
    waiter_a = asyncio.create_task(obj.work())
    waiter_b = asyncio.create_task(obj.work())
    await asyncio.sleep(0)
    obj.release.set()
    assert await leader == 42
    assert await waiter_a == 42
    assert await waiter_b == 42
    assert obj.call_count == 1
    assert obj._fut is None


@pytest.mark.asyncio
async def test_coalesce_concurrent_future_propagates_exception_to_waiters() -> None:
    """Leader exception is observed by every concurrent waiter."""
    obj = _Coalesced()
    obj.raise_exc = RuntimeError("boom")
    leader = asyncio.create_task(obj.work())
    await obj.started.wait()
    waiter = asyncio.create_task(obj.work())
    await asyncio.sleep(0)
    obj.release.set()
    with pytest.raises(RuntimeError, match="boom"):
        await leader
    with pytest.raises(RuntimeError, match="boom"):
        await waiter
    assert obj._fut is None


@pytest.mark.asyncio
async def test_coalesce_concurrent_future_leader_cancellation_surfaces_to_waiters() -> (
    None
):
    """Leader cancellation propagates to waiters, future is cleared."""
    obj = _Coalesced()
    leader = asyncio.create_task(obj.work())
    await obj.started.wait()
    waiter = asyncio.create_task(obj.work())
    await asyncio.sleep(0)
    leader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leader
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert obj._fut is None


@pytest.mark.asyncio
async def test_coalesce_concurrent_future_waiter_cancel_does_not_strand_leader() -> (
    None
):
    """Cancelling a waiter does not poison the shared future."""
    obj = _Coalesced()
    leader = asyncio.create_task(obj.work())
    await obj.started.wait()
    waiter_a = asyncio.create_task(obj.work())
    waiter_b = asyncio.create_task(obj.work())
    await asyncio.sleep(0)
    waiter_a.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter_a
    obj.release.set()
    assert await leader == 42
    assert await waiter_b == 42
    assert obj._fut is None


@pytest.mark.asyncio
async def test_coalesce_concurrent_future_resets_between_sequential_calls() -> None:
    """Future state resets so a later call runs fresh."""
    obj = _Coalesced()
    obj.release.set()
    assert await obj.work() == 42
    assert obj._fut is None
    obj.return_value = 7
    assert await obj.work() == 7
    assert obj.call_count == 2


@pytest.mark.asyncio
async def test_coalesce_concurrent_future_requires_attribute_initialised() -> None:
    """Missing attribute on the instance raises AttributeError."""

    class _Missing:
        @coalesce_concurrent_future("_fut")
        async def work(self) -> int:
            return 1

    with pytest.raises(AttributeError):
        await _Missing().work()
