"""Recheck legacy stream membership while waiting and before each event."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

from app.features.chat.repository import require_membership
from app.infra.async_stream import close_reader
from app.infra.owned_work import run_owned

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Coroutine

    from app.api.stream_credentials import StreamCredential
    from app.features.assistant.event_protocol import AssistantEvent
    from app.infra.async_stream import CloseableAsyncIterator
    from app.infra.extensions import SessionFactory

POLL_SECONDS = 1.0


async def authorized_events(
    iterator: AsyncIterator[AssistantEvent],
    session_factory: SessionFactory,
    workspace_id: int,
    user_id: int,
    credential: StreamCredential | None = None,
) -> AsyncGenerator[AssistantEvent, None]:
    """Recheck membership before each event and close the iterator on exit."""
    pending: asyncio.Task[AssistantEvent] | None = None
    failed = False

    async def check() -> None:
        await run_owned(
            require_membership, session_factory, workspace_id, user_id, credential,
        )

    try:
        await check()
        while True:
            pending = asyncio.create_task(
                cast("Coroutine[Any, Any, AssistantEvent]", anext(iterator)),
            )
            while True:
                ready, _ = await asyncio.wait({pending}, timeout=POLL_SECONDS)
                await check()
                if ready:
                    break
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
        cancelled = await close_reader(
            pending, cast("CloseableAsyncIterator[AssistantEvent]", iterator),
        )
        if cancelled and not failed:
            raise asyncio.CancelledError
