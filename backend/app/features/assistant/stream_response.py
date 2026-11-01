"""Close prepared runs even when ASGI fails before entering their iterator."""

import asyncio
from collections.abc import AsyncIterator, Callable, Generator, Mapping
from contextlib import contextmanager
from typing import TYPE_CHECKING, TypeVar, cast

import anyio
from loguru import logger
from runtime_common.http_stream import close_reader, drain_cleanup
from sqlalchemy.orm import Session
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from app.features.assistant import repository
from app.features.assistant.service import cleanup_stream_session
from app.features.assistant.stream_persistence import StreamWorker
from app.infra.extensions import SessionFactory

if TYPE_CHECKING:
    from runtime_common.async_stream import AsyncClosableIterator

T = TypeVar("T")
RunScope = tuple[int, int, int, int]


@contextmanager
def preparation_session(
    session_factory: SessionFactory,
) -> Generator[Session, None, None]:
    """Yield the preparation Session and always close it on its owner."""
    session = session_factory()
    try:
        yield session
    finally:
        cleanup_stream_session(session, "close")


async def prepare_response_run(
    prepare: Callable[[Callable[[RunScope], None]], T],
    session_factory: SessionFactory,
) -> T:
    """Drain SQL preparation before compensating an interrupted HTTP request."""
    created_scope: RunScope | None = None

    def capture(scope: RunScope) -> None:
        nonlocal created_scope
        created_scope = scope

    async with StreamWorker() as worker:
        try:
            return await worker.call(prepare, capture)
        except BaseException:
            if created_scope is not None:
                with anyio.CancelScope(shield=True):
                    await worker.call(
                        cleanup_unclaimed_run,
                        session_factory,
                        created_scope,
                    )
            raise


def cleanup_unclaimed_run(
    session_factory: SessionFactory,
    scope: RunScope,
) -> None:
    """Compensate a prepared run whose HTTP request never started streaming."""
    # Internal compensation uses exact prepared identities, and can still run
    # after membership removal. This is not a user-facing cancellation API.
    session = None
    try:
        session = session_factory()
        (
            workspace_id,
            user_id,
            conversation_id,
            run_id,
        ) = scope
        repository.cancel_unclaimed_run(
            session,
            workspace_id,
            user_id,
            conversation_id,
            run_id,
        )
        session.commit()
    except Exception as exc:
        logger.error(
            "Assistant unclaimed run cleanup failed (error_type={})",
            type(exc).__name__,
        )
        if session is not None:
            cleanup_stream_session(session, "rollback")
    finally:
        if session is not None:
            cleanup_stream_session(session, "close")


class PreparedStreamResponse(StreamingResponse):
    """Streaming response that releases an unclaimed prepared run."""

    def __init__(
        self,
        content: AsyncIterator[str | bytes],
        *,
        session_factory: SessionFactory,
        run_scope: RunScope,
        media_type: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        """Bind the response to the identities it must compensate."""
        super().__init__(content, media_type=media_type, headers=headers)
        self.session_factory = session_factory
        self.run_scope = run_scope

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Stream content and release the prepared run in every outcome."""
        failed = False
        try:
            await super().__call__(scope, receive, send)
        except BaseException:
            failed = True
            raise
        finally:
            async def release() -> None:
                iterator = cast(
                    "AsyncClosableIterator[str | bytes]",
                    self.body_iterator,
                )
                await close_reader(None, iterator)
                async with StreamWorker() as worker:
                    await worker.call(
                        cleanup_unclaimed_run,
                        self.session_factory,
                        self.run_scope,
                    )

            cancelled = await drain_cleanup(release())
            if cancelled and not failed:
                raise asyncio.CancelledError
