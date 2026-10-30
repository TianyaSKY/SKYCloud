"""Worker-owned MCP session cleanup; callers retain all commit/rollback decisions."""

from collections.abc import Generator
from contextlib import contextmanager

from loguru import logger
from sqlalchemy.orm import Session

from app.infra.extensions import SessionFactory


@contextmanager
def worker_session(session_factory: SessionFactory) -> Generator[Session, None, None]:
    """Yield a worker-owned session and always dispose of it afterwards."""
    session = session_factory()
    try:
        yield session
    finally:
        try:
            session.close()
        except Exception as exc:
            # Session/pool/driver cleanup can raise arbitrary exceptions. Keep
            # the caller's result or original failure, then discard the handle.
            logger.error(
                "MCP session close failed: error_type={}", type(exc).__name__,
            )
            # Discard the connection rather than returning a failed cleanup to
            # the pool. Invalidation also rolls back any uncommitted work.
            try:
                session.invalidate()
            except Exception as cleanup_exc:
                # A final driver invalidation failure must likewise never
                # replace the business outcome or disclose connection details.
                logger.error(
                    "MCP session invalidation failed: error_type={}",
                    type(cleanup_exc).__name__,
                )
