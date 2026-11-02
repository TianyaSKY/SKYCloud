"""文件检索编排层: Worker 专属 SQL、缓存与共享向量嵌入服务.

上传、对象发布及分片生命周期全权交由文件服务处理。
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
    """单次文件搜索的检索词、分页参数与搜索模式."""

    query: str
    page: int = 1
    page_size: int = 10
    search_type: str = "fuzzy"


def _empty_page(params: SearchFilesParams) -> dict[str, object]:
    """基于传入的分页参数返回空的分页搜索结果对象."""
    return {"items": [], "total": 0, "page": params.page, "page_size": params.page_size}


async def _execute_search(
    session: Session | None,
    workspace_id: int,
    *,
    session_factory: Callable[[], AbstractContextManager[Session]] | None,
    user_id: int | None,
    run: Callable[[Session], ResultT] | None,
) -> ResultT | None:
    """完成成员鉴权后在 Session 所属线程上运行搜索回调."""

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
    """在工作空间内根据文件名模糊匹配或向量嵌入相似度执行检索."""
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
            # 在调用上游模型供应商资源前优先完成鉴权校验。
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
    """在调用时动态读取模块级网关函数, 便于单元测试进行替换."""
    return _search_cache_allowed(session, workspace_id, query, page, page_size)


def _search_revision(session: Session, workspace_id: int) -> int:
    """返回持久化在 SQL 中的工作空间搜索缓存版本号."""
    revision = session.scalar(
        select(Workspace.search_cache_revision).where(Workspace.id == workspace_id),
    )
    return revision or 0


def _search_generation_key(
    session: Session, workspace_id: int, query: str, page: int, page_size: int,
) -> str:
    """返回工作空间搜索缓存的代际作用域键名."""
    del session, query, page, page_size
    return f"{SEARCH_GENERATION_PREFIX}:{workspace_id}"


def _search_cache_key(
    session: Session,
    workspace_id: int,
    query: str,
    page: int,
    page_size: int,
) -> str:
    """基于当前入参计算模糊搜索的 Redis 缓存键名."""
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
    """返回文件名与关键词匹配的单页文件列表."""
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
    """按余弦距离升序返回与目标向量相似的单页文件列表."""
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
    """计算单段文本的向量嵌入; 设计约定发生失败时返回空列表."""
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
