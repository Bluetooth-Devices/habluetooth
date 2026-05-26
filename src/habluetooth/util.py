"""The bluetooth utilities."""

from __future__ import annotations

import asyncio
import functools
from contextlib import suppress
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar

from bluetooth_auto_recovery import recover_adapter

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

_P = ParamSpec("_P")
_R = TypeVar("_R")


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


def coalesce_concurrent_future(
    attr: str,
) -> Callable[
    [Callable[_P, Coroutine[Any, Any, _R]]],
    Callable[_P, Coroutine[Any, Any, _R]],
]:
    """
    Coalesce concurrent async method calls onto a single shared future.

    Mirrors the home-assistant ``loader.py`` shared-future pattern. The
    first caller runs the wrapped coroutine and the result (or exception)
    is published on a future stored at ``self.<attr>``; concurrent callers
    wait on the same future and observe the same outcome. ``asyncio.wait``
    is used on the waiter side so a cancelled waiter does not transitively
    cancel the shared future and strand the leader or its siblings.

    Pre-condition: ``self.<attr>`` must already exist on the instance and
    be initialised to ``None`` before the first call. The decorator reads
    it via ``getattr`` (no default) and resets it to ``None`` in ``finally``
    once the leader completes. Only usable on instance methods — ``self``
    is taken from ``args[0]``.
    """

    def decorator(
        func: Callable[_P, Coroutine[Any, Any, _R]],
    ) -> Callable[_P, Coroutine[Any, Any, _R]]:
        @functools.wraps(func)
        async def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            self = args[0]
            future: asyncio.Future[_R] | None = getattr(self, attr)
            if future is not None:
                await asyncio.wait((future,))
                return future.result()
            future = asyncio.get_running_loop().create_future()
            setattr(self, attr, future)
            try:
                result = await func(*args, **kwargs)
            except BaseException as ex:
                future.set_exception(ex)
                # Mark the exception as retrieved so asyncio does not warn
                # when no concurrent waiters consume it.
                with suppress(BaseException):
                    future.result()
                raise
            else:
                future.set_result(result)
                return result
            finally:
                setattr(self, attr, None)

        return wrapper

    return decorator
