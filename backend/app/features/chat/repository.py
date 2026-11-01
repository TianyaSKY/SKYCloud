"""Worker-owned, currently authorized pgvector chunk retrieval."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

from loguru import logger
from sqlalchemy import Row, text

from app.exceptions import PermissionDeniedError
from app.features.workspace.permissions import assert_member_locked

if TYPE_CHECKING:
    from collections.abc import Generator, Sequence
    from typing import Any

    from sqlalchemy.orm import Session

    from app.infra.extensions import SessionFactory


def vector_rows(
    session_factory: SessionFactory,
    vector: list[float],
    workspace_id: int,
    user_id: int,
    limit: int,
) -> Sequence[Row[Any]]:
    """Return workspace chunks nearest to ``vector`` for an authorized member."""
    if type(user_id) is not int or user_id <= 0:
        message = "Authenticated retrieval identity is required"
        raise PermissionDeniedError(message)
    with _owned_session(session_factory) as session:
        # Hold lifecycle identity/membership locks only during SQL, never model IO.
        assert_member_locked(session, workspace_id, user_id)
        return session.execute(text("""
            SELECT c.id, f.id, f.name, c.content, f.mime_type, c.chunk_index,
                   c.page_number, (c.vector_info <=> :vector) AS distance
            FROM file_chunks c JOIN files f ON f.id = c.file_id
            WHERE c.workspace_id = :workspace_id AND f.workspace_id = :workspace_id
              AND c.vector_info IS NOT NULL
            ORDER BY c.vector_info <=> :vector LIMIT :limit
        """), {"vector": str(vector), "workspace_id": workspace_id,
               "limit": limit}).fetchall()


@contextmanager
def _owned_session(session_factory: SessionFactory) -> Generator[Session, None, None]:
    """Yield one Session and close it on the thread that executed the query."""
    session = session_factory()
    try:
        yield session
    finally:
        try:
            session.close()
        except Exception as exc:
            logger.error(
                "Chat retrieval session close failed (error_type={})",
                type(exc).__name__,
            )
            try:
                session.invalidate()
            except Exception as invalidation_exc:
                logger.error(
                    "Chat retrieval session invalidation failed (error_type={})",
                    type(invalidation_exc).__name__,
                )


