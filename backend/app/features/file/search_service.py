"""File retrieval orchestration: worker-owned SQL, cache and shared embedding adapter.

Upload, object publication and multipart lifecycle belong to the file service.
"""

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import TypeVar

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.exceptions import PermissionDeniedError, ServiceOperationError
from app.features.file import repository
from app.features.workspace.permissions import assert_member
from app.infra.cache import CacheableParams, cacheable, sql_cache_allowed
from app.infra.llm.client import embed_texts
from app.infra.llm.config import get_embedding_model_config
from app.infra.owned_work import run_owned
from app.models.workspace import Workspace

CACHE_EXPIRATION = 3600
SEARCH_CACHE_PREFIX = "search:fuzzy"
SEARCH_GENERATION_PREFIX = "cache:generation:search:fuzzy"

ResultT = TypeVar("ResultT")


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchFilesParams:
    """Search term, paging and mode for one file search."""

    query: str
    page: int = 1
    page_size: int = 10
    search_type: str = "fuzzy"


def _empty_page(params: SearchFilesParams) -> dict[str, object]:
    """Return the empty result page with the requested paging values."""
    return {"items": [], "total": 0, "page": params.page, "page_size": params.page_size}


async def _execute_search(
    session: Session | None,
    workspace_id: int,
    *,
    session_factory: Callable[[], AbstractContextManager[Session]] | None,
    user_id: int | None,
    run: Callable[[Session], ResultT] | None,
) -> ResultT | None:
    """Run the search callback on the session owner thread after authorization."""

    def work() -> ResultT | None:
        if session_factory is not None:
            with session_factory() as owned_session:
                if user_id is not None:
                    assert_member(owned_session, workspace_id, user_id)
                return run(owned_session) if run is not None else None
        if session is None:
            message = "A session is required when no session_factory is provided"
            raise ServiceOperationError(message)
        if user_id is not None:
            assert_member(session, workspace_id, user_id)
        return run(session) if run is not None else None

    return await run_owned(work)


async def search_files(
    session: Session | None,
    workspace_id: int,
    params: SearchFilesParams,
    *,
    session_factory: Callable[[], AbstractContextManager[Session]] | None = None,
    user_id: int | None = None,
) -> dict[str, object]:
    """Search file names or embeddings inside the workspace."""
    if not params.query:
        return _empty_page(params)

    if params.search_type not in {"vector", "semantic"}:
        fuzzy = await _execute_search(
            session,
            workspace_id,
            session_factory=session_factory,
            user_id=user_id,
            run=lambda owned: _search_files_fuzzy(
                owned, workspace_id, params.query, params.page, params.page_size,
            ),
        )
        return _empty_page(params) if fuzzy is None else fuzzy

    try:
        if user_id is not None:
            # Authorize before spending provider resources.
            await _execute_search(
                session,
                workspace_id,
                session_factory=session_factory,
                user_id=user_id,
                run=None,
            )
        embeddings = await embedding_desc(
            params.query, get_embedding_model_config(), user_id=user_id or 0,
        )
        vector = await _execute_search(
            session,
            workspace_id,
            session_factory=session_factory,
            user_id=user_id,
            run=lambda owned: _search_files_vector_by_embedding(
                owned, workspace_id, embeddings, params.page, params.page_size,
            ),
        )
    except (PermissionDeniedError, PermissionError):
        raise
    except Exception as e:
        logger.error("Vector search error (error_type={})", type(e).__name__)
        return {
            "items": [],
            "total": 0,
            "page": params.page,
            "page_size": params.page_size,
            "error": "Vector search is temporarily unavailable",
        }
    if vector is None:
        return _empty_page(params)
    return vector


_search_cache_allowed: Callable[..., bool] = sql_cache_allowed


def _cache_allowed(
    session: Session, workspace_id: int, query: str, page: int, page_size: int,
) -> bool:
    """Consult the module-level gate at call time so tests can replace it."""
    return _search_cache_allowed(session, workspace_id, query, page, page_size)


def _search_revision(session: Session, workspace_id: int) -> int:
    """Return the workspace search revision stored in SQL."""
    revision = session.scalar(
        select(Workspace.search_cache_revision).where(Workspace.id == workspace_id),
    )
    return revision or 0


def _search_generation_key(
    session: Session, workspace_id: int, query: str, page: int, page_size: int,
) -> str:
    """Return the cache generation scope of the workspace search."""
    del session, query, page, page_size
    return f"{SEARCH_GENERATION_PREFIX}:{workspace_id}"


def _search_cache_key(
    session: Session,
    workspace_id: int,
    query: str,
    page: int,
    page_size: int,
) -> str:
    """Return the fuzzy search cache key for these arguments."""
    revision = _search_revision(session, workspace_id)
    return (
        f"{workspace_id}:sql:{revision}:{query}:{page}:{page_size}:fuzzy"
    )


@cacheable(
    CacheableParams(prefix=SEARCH_CACHE_PREFIX, expire=CACHE_EXPIRATION),
    cache_allowed=_cache_allowed,
    generation_key=_search_generation_key,
    key=_search_cache_key,
)
def _search_files_fuzzy(
    session: Session, workspace_id: int, query: str, page: int, page_size: int,
) -> dict[str, object]:
    """Return one page of files whose name matches the term."""
    rows = repository.search_names(session, workspace_id, query, page, page_size)
    return {
        "items": [file.to_dict() for file in rows.items],
        "total": rows.total,
        "page": page,
        "page_size": page_size,
    }


def _search_files_vector_by_embedding(
    session: Session,
    workspace_id: int,
    embeddings: list[float],
    page: int,
    page_size: int,
) -> dict[str, object]:
    """Return one page of embedded files ordered by cosine distance."""
    if not embeddings:
        return {
            "items": [],
            "total": 0,
            "page": page,
            "page_size": page_size,
            "error": "No embeddings returned",
        }

    rows = repository.search_vectors(session, workspace_id, embeddings, page, page_size)
    return {
        "items": [file.to_dict() for file in rows.items],
        "total": rows.total,
        "page": page,
        "page_size": page_size,
    }


async def embedding_desc(
    desc: str, config: dict[str, str], user_id: int = 0,
) -> list[float]:
    """Return one text embedding; failures return an empty list by design."""
    try:
        vectors = await embed_texts(
            texts=desc,
            config=config,
            user_id=user_id,
            query_summary=desc[:100] if desc else None,
        )
    except Exception as e:
        logger.error("Embedding failed (error_type={})", type(e).__name__)
        return []
    return vectors[0] if vectors else []
