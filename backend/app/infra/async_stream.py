"""Cancellation-safe teardown of an owned async iterator and its active read."""

import asyncio
from collections.abc import Awaitable, Coroutine
from typing import Protocol, Self, TypeVar

import anyio
from loguru import logger

_T = TypeVar("_T")
_T_co = TypeVar("_T_co", covariant=True)


class CloseableAsyncIterator(Protocol[_T_co]):
    """An async iterator whose owner can close it explicitly."""

    def __aiter__(self) -> Self:
        """Return this iterator."""
        ...

    def __anext__(self) -> Awaitable[_T_co]:
        """Return the next item."""
        ...

    async def aclose(self) -> None:
        """Close the owned iterator."""
        ...


async def close_reader(
    pending: asyncio.Task[_T] | None,
    iterator: CloseableAsyncIterator[_T],
) -> bool:
    """Drain the owned read before closing its iterator, despite repeat cancellation."""

    async def release() -> None:
        if pending is not None:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        try:
            await iterator.aclose()
        except (Exception, asyncio.CancelledError) as exc:
            logger.error(
                "Stream iterator close failed (error_type={})", type(exc).__name__,
            )

    return await drain_cleanup(release())


async def drain_cleanup(operation: Coroutine[object, object, object]) -> bool:
    """Wait for owned teardown without cancelling it when its caller is cancelled."""
    cancelled = False
    with anyio.CancelScope(shield=True):
        released = asyncio.create_task(operation)
        while not released.done():
            try:
                await asyncio.shield(released)
            except asyncio.CancelledError:
                cancelled = True
        released.result()
    return cancelled
