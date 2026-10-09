"""Worker-owned, currently authorized pgvector chunk retrieval."""
from loguru import logger
from sqlalchemy import text
from app.exceptions import PermissionDeniedError
from app.features.workspace.permissions import assert_member_locked


def vector_rows(session_factory, vector, workspace_id: int, user_id: int, limit: int):
    if type(user_id) is not int or user_id <= 0:
        raise PermissionDeniedError("Authenticated retrieval identity is required")
    session = session_factory()
    try:
        # Hold lifecycle identity/membership locks only during SQL, never model IO.
        assert_member_locked(session, workspace_id, user_id)
        return session.execute(text("""
            SELECT c.id, f.id, f.name, c.content, f.mime_type, c.chunk_index,
                   c.page_number, (c.vector_info <=> :vector) AS distance
            FROM file_chunks c JOIN files f ON f.id = c.file_id
            WHERE c.workspace_id = :workspace_id AND f.workspace_id = :workspace_id
              AND c.vector_info IS NOT NULL
            ORDER BY c.vector_info <=> :vector LIMIT :limit
        """), {"vector":str(vector), "workspace_id":workspace_id, "limit":limit}).fetchall()
    finally:
        try:
            session.close()
        except Exception as exc:
            logger.error("Chat retrieval session close failed (error_type={})", type(exc).__name__)
            try:
                session.invalidate()
            except Exception as invalidation_exc:
                logger.error("Chat retrieval session invalidation failed (error_type={})", type(invalidation_exc).__name__)
