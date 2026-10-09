"""Recheck legacy stream membership while waiting and before each event."""
import asyncio
from app.infra.async_stream import close_reader
from app.infra.owned_work import run_owned
from app.features.chat.repository import require_membership

POLL_SECONDS = 1.0


async def authorized_events(iterator, session_factory, workspace_id: int, user_id: int, credential=None):
    pending = None
    failed = False
    async def check():
        await run_owned(require_membership, session_factory, workspace_id, user_id, credential)
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
    except BaseException:
        failed = True
        raise
    finally:
        cancelled = await close_reader(pending, iterator)
        if cancelled and not failed:
            raise asyncio.CancelledError()
