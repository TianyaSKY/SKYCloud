"""Worker-owned MCP session cleanup; callers retain all commit/rollback decisions."""
from contextlib import contextmanager
from loguru import logger


@contextmanager
def worker_session(session_factory):
    session = session_factory()
    try:
        yield session
    finally:
        try:
            session.close()
        except Exception as exc:
            logger.error('MCP session close failed: error_type={}', type(exc).__name__)
            # Discard the connection rather than returning a failed cleanup to
            # the pool. Invalidation also rolls back any uncommitted work.
            try:
                session.invalidate()
            except Exception as cleanup_exc:
                logger.error('MCP session invalidation failed: error_type={}', type(cleanup_exc).__name__)
