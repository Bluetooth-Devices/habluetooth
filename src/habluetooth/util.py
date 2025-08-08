"""The bluetooth utilities."""

import asyncio
from collections.abc import Coroutine
from functools import cache
from pathlib import Path
from typing import Any, TypeVar

from bluetooth_auto_recovery import recover_adapter

_T = TypeVar("_T")


async def async_reset_adapter(
    adapter: str | None, mac_address: str, gone_silent: bool
) -> bool | None:
    """Reset the adapter."""
    if adapter and adapter.startswith("hci"):
        adapter_id = int(adapter[3:])
        return await recover_adapter(adapter_id, mac_address, gone_silent)
    return False


@cache
def is_docker_env() -> bool:
    """Return True if we run in a docker env."""
    return Path("/.dockerenv").exists()


def create_eager_task(
    coro: Coroutine[Any, Any, _T],
    *,
    name: str | None = None,
    loop: asyncio.AbstractEventLoop | None = None,
) -> asyncio.Task[_T]:
    """Create a task from a coroutine and schedule it to run immediately."""
    return asyncio.Task(
        coro,
        loop=loop or asyncio.get_running_loop(),
        name=name,
        eager_start=True,  # type: ignore[call-arg]
    )
