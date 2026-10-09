"""Close prepared runs even when ASGI fails before entering their iterator."""
import anyio
from loguru import logger
from starlette.responses import StreamingResponse

from app.features.assistant import repository
from app.features.assistant.stream_persistence import StreamWorker


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
            # Starlette's disconnect task cancels its AnyIO scope. Shield the
            # iterator teardown and SQL compensation from that cancellation.
            with anyio.CancelScope(shield=True):
                try:
                    await self.body_iterator.aclose()
                except Exception as exc:
                    logger.error("Assistant response iterator close failed (error_type={})", type(exc).__name__)
                finally:
                    async with StreamWorker() as worker:
                        await worker.call(cleanup_unclaimed_run, self.session_factory, self.run_scope)
