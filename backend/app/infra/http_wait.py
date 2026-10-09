"""Bounded admission waits with ongoing credential checks and drained cleanup."""
import asyncio
import time
import anyio
from fastapi import HTTPException
from loguru import logger
from app.infra.async_stream import drain_cleanup


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
        async def abort():
            if on_abort is not None:
                try:
                    await on_abort()
                except (Exception, asyncio.CancelledError) as exc:
                    logger.error("HTTP admission cleanup failed (error_type={})", type(exc).__name__)
        async def cleanup():
            if not pending.done():
                pending.cancel()
            try:
                if interrupted:
                    await abort()
            finally:
                await asyncio.gather(pending, return_exceptions=True)
        cancelled = await drain_cleanup(cleanup())
        if not interrupted:
            try:
                if cancelled:
                    raise asyncio.CancelledError()
                # An outer AnyIO scope may already be cancelled, with no
                # checkpoint outside shielded cleanup to deliver it yet.
                await anyio.lowlevel.checkpoint_if_cancelled()
            except asyncio.CancelledError:
                await drain_cleanup(abort())
                raise
