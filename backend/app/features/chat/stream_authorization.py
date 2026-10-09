"""Recheck legacy stream membership while waiting and before each event."""
import asyncio
import sys
from app.infra.async_stream import close_reader
from app.features.chat.repository import require_membership

POLL_SECONDS = 1.0


async def authorized_events(iterator, session_factory, workspace_id: int, user_id: int, credential=None):
    pending = None
    async def check():
        await asyncio.to_thread(require_membership, session_factory, workspace_id, user_id, credential)
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
        original = sys.exception()
        cancelled = await close_reader(pending, iterator)
        if cancelled and original is None:
            raise asyncio.CancelledError()
