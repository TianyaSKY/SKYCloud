"""File retrieval orchestration: worker-owned SQL, cache and shared embedding adapter.

Upload, object publication and multipart lifecycle belong to the file service.
"""
from app.infra.owned_work import run_owned
from typing import Any
from sqlalchemy.orm import Session
from sqlalchemy import select
from app.models.workspace import Workspace
from loguru import logger
from app.exceptions import PermissionDeniedError, ServiceOperationError
from app.infra.cache import cacheable, sql_cache_allowed
from app.infra.llm.config import get_embedding_model_config
from app.features.file import repository
from app.features.workspace.permissions import assert_member

CACHE_EXPIRATION = 3600
SEARCH_CACHE_PREFIX = 'search:fuzzy'
SEARCH_GENERATION_PREFIX = 'cache:generation:search:fuzzy'


async def search_files(
        session: Session | None,
        workspace_id: int,
        query: str,
        page: int = 1,
        page_size: int = 10,
        search_type: str = "fuzzy",
        *, session_factory=None, user_id: int | None = None,
) -> dict[str, Any]:
    async def execute(function=None, *args):
        def work():
            if session_factory is not None:
                with session_factory() as owned_session:
                    if user_id is not None:
                        assert_member(owned_session, workspace_id, user_id)
                    return function(owned_session, *args) if function is not None else None
            if session is None:
                raise ServiceOperationError("A session is required when no session_factory is provided")
            if user_id is not None:
                assert_member(session, workspace_id, user_id)
            return function(session, *args) if function is not None else None
        return await run_owned(work)

    if not query:
        return {"items": [], "total": 0, "page": page, "page_size": page_size}

    if search_type not in {"vector", "semantic"}:
        return await execute(_search_files_fuzzy, workspace_id, query, page, page_size)

    try:
        if user_id is not None:
            await execute()  # Authorize before spending provider resources.
        embeddings = await embedding_desc(query, get_embedding_model_config(), user_id=user_id or 0)
        return await execute(_search_files_vector_by_embedding, workspace_id, embeddings, page, page_size)
    except (PermissionDeniedError, PermissionError):
        raise
    except Exception as e:
        logger.error("Vector search error (error_type={})", type(e).__name__)
        return {
            "items": [], "total": 0, "page": page, "page_size": page_size,
            "error": "Vector search is temporarily unavailable",
        }


_search_cache_allowed = sql_cache_allowed


def _search_revision(session, workspace_id):
    return session.scalar(select(Workspace.search_cache_revision).where(Workspace.id == workspace_id)) or 0


@cacheable(
    prefix=SEARCH_CACHE_PREFIX,
    expire=CACHE_EXPIRATION,
    cache_allowed=lambda *args, **kwargs: _search_cache_allowed(*args, **kwargs),
    generation_key=lambda session, workspace_id, *args, **kwargs: f"{SEARCH_GENERATION_PREFIX}:{workspace_id}",
    key=lambda session, workspace_id, query, page, page_size, **_: f"{workspace_id}:sql:{_search_revision(session, workspace_id)}:{query}:{page}:{page_size}:fuzzy",
)
def _search_files_fuzzy(
        session: Session,
        workspace_id: int, query: str, page: int, page_size: int
) -> dict[str, Any]:
    rows = repository.search_names(session, workspace_id, query, page, page_size)
    return {"items": [file.to_dict() for file in rows.items], "total": rows.total,
            "page": page, "page_size": page_size}


def _search_files_vector_by_embedding(
        session: Session,
        workspace_id: int, embeddings: list[float], page: int, page_size: int
) -> dict[str, Any]:
    if not embeddings:
        return {
            "items": [],
            "total": 0,
            "page": page,
            "page_size": page_size,
            "error": "No embeddings returned",
        }

    rows = repository.search_vectors(session, workspace_id, embeddings, page, page_size)
    return {"items": [file.to_dict() for file in rows.items], "total": rows.total,
            "page": page, "page_size": page_size}


async def embedding_desc(desc: str, config: dict[str, str], user_id: int = 0) -> list[float]:
    """单条文本 embedding；失败返回空列表，避免索引任务硬失败。"""
    try:
        from app.infra.llm.client import embed_texts
        vectors = await embed_texts(
            texts=desc,
            config=config,
            user_id=user_id,
            query_summary=desc[:100] if desc else None,
        )
        return vectors[0] if vectors else []
    except Exception as e:
        logger.error("Embedding failed (error_type={})", type(e).__name__)
        return []
