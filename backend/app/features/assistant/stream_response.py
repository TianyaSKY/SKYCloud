"""Close prepared runs even when ASGI fails before entering their iterator."""
import asyncio
import sys
import anyio
from contextlib import contextmanager
from loguru import logger
from starlette.responses import StreamingResponse

from app.infra.async_stream import close_reader, drain_cleanup
from app.features.assistant import repository
from app.features.assistant.stream_persistence import StreamWorker


@contextmanager
def preparation_session(session_factory):
    session = session_factory()
    try:
        yield session
    finally:
        from app.features.assistant.service import _cleanup_stream_session
        _cleanup_stream_session(session, "close")


async def prepare_response_run(prepare, session_factory):
    """Drain SQL preparation before compensating an interrupted HTTP request."""
    created_scope = None
    def capture(scope):
        nonlocal created_scope
        created_scope = scope
    async with StreamWorker() as worker:
        try:
            return await worker.call(prepare, capture)
        except BaseException:
            if created_scope is not None:
                with anyio.CancelScope(shield=True):
                    await worker.call(cleanup_unclaimed_run, session_factory, created_scope)
            raise


def cleanup_unclaimed_run(session_factory, scope):
    # Internal compensation uses exact prepared identities, and can still run
    # after membership removal. This is not a user-facing cancellation API.
    session = None
    try:
        session = session_factory()
        repository.cancel_unclaimed_run(session, *scope)
        session.commit()
    except Exception as exc:
        logger.error("Assistant unclaimed run cleanup failed (error_type={})", type(exc).__name__)
        if session is not None:
            from app.features.assistant.service import _cleanup_stream_session
            _cleanup_stream_session(session, "rollback")
    finally:
        if session is not None:
            from app.features.assistant.service import _cleanup_stream_session
            _cleanup_stream_session(session, "close")


class PreparedStreamResponse(StreamingResponse):
    def __init__(self, content, *, session_factory, run_scope, **kwargs):
        super().__init__(content, **kwargs)
        self.session_factory = session_factory
        self.run_scope = run_scope

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            original = sys.exception()
            async def release():
                await close_reader(None, self.body_iterator)
                async with StreamWorker() as worker:
                    await worker.call(cleanup_unclaimed_run, self.session_factory, self.run_scope)
            cancelled = await drain_cleanup(release())
            if cancelled and original is None:
                raise asyncio.CancelledError()
