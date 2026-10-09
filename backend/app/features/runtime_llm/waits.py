"""Bounded admission waits with ongoing credential checks and drained cleanup."""
import asyncio
import time
import anyio
from fastapi import HTTPException
from loguru import logger


async def guarded_wait(operation, *, check, seconds, status, detail, interval=1.0, on_abort=None):
    pending = asyncio.create_task(operation)
    deadline = time.monotonic() + seconds
    interrupted = False
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HTTPException(status, detail)
            ready, _ = await asyncio.wait({pending}, timeout=min(interval, remaining))
            await check()
            if time.monotonic() >= deadline:
                raise HTTPException(status, detail)
            if ready:
                return pending.result()
    except BaseException:
        interrupted = True
        raise
    finally:
        async def cleanup():
            if not pending.done():
                pending.cancel()
            try:
                if interrupted and on_abort is not None:
                    try:
                        await on_abort()
                    except (Exception, asyncio.CancelledError) as exc:
                        logger.error("Model admission cleanup failed (error_type={})", type(exc).__name__)
            finally:
                await asyncio.gather(pending, return_exceptions=True)
        released = asyncio.create_task(cleanup())
        with anyio.CancelScope(shield=True):
            while not released.done():
                try:
                    await asyncio.shield(released)
                except asyncio.CancelledError:
                    continue
        released.result()
