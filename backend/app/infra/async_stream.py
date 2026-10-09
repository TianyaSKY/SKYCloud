"""Cancellation-safe teardown of an owned async iterator and its active read."""
import asyncio
import anyio
from loguru import logger


async def close_reader(pending, iterator):
    """Drain the owned read before closing its iterator, despite repeat cancellation."""
    async def release():
        if pending is not None:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        try:
            await iterator.aclose()
        except (Exception, asyncio.CancelledError) as exc:
            logger.error("Stream iterator close failed (error_type={})", type(exc).__name__)
    return await drain_cleanup(release())


async def drain_cleanup(operation):
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
