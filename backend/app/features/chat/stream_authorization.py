"""Recheck legacy stream membership while waiting and before each event."""
import asyncio
from contextlib import aclosing
from app.features.chat.repository import require_membership

POLL_SECONDS = 1.0


async def authorized_events(iterator, session_factory, workspace_id: int, user_id: int):
    pending = None
    async def check():
        await asyncio.to_thread(require_membership, session_factory, workspace_id, user_id)
    async with aclosing(iterator):
        try:
            await check()
            while True:
                pending = asyncio.create_task(anext(iterator))
                while True:
                    ready, _ = await asyncio.wait({pending}, timeout=POLL_SECONDS)
                    await check()
                    if ready: break
                try:
                    event = pending.result()
                except StopAsyncIteration:
                    return
                pending = None
                yield event
        finally:
            # Drain an active anext before aclosing the generator.
            if pending is not None:
                if not pending.done(): pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
