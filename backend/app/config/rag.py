"""RAG 检索与重排序参数强类型配置."""

import os
from dataclasses import dataclass
from typing import Final

DEFAULT_RAG_VECTOR_FETCH_K: Final[int] = 20
DEFAULT_RERANK_TOP_K: Final[int] = 8
DEFAULT_RAG_MULTI_QUERY_MAX_QUERIES: Final[int] = 3
DEFAULT_RAG_RRF_K: Final[int] = 60
DEFAULT_RAG_FUSION_TOP_K: Final[int] = 30


def _read_rag_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        val = int(raw.strip())
    except (TypeError, ValueError):
        return default
    else:
        return val if val > 0 else default


@dataclass(frozen=True, slots=True)
class RagConfig:
    """RAG 检索、多查询扩充与重排序参数."""

    vector_fetch_k: int = DEFAULT_RAG_VECTOR_FETCH_K
    rerank_top_k: int = DEFAULT_RERANK_TOP_K
    multi_query_max_queries: int = DEFAULT_RAG_MULTI_QUERY_MAX_QUERIES
    rrf_k: int = DEFAULT_RAG_RRF_K
    fusion_top_k: int = DEFAULT_RAG_FUSION_TOP_K

    def __post_init__(self) -> None:
        """校验 RAG 参数边界约束."""
        if self.vector_fetch_k <= 0:
            msg = "vector_fetch_k must be > 0"
            raise ValueError(msg)
        if self.rerank_top_k <= 0:
            msg = "rerank_top_k must be > 0"
            raise ValueError(msg)
        if self.multi_query_max_queries <= 0:
            msg = "multi_query_max_queries must be > 0"
            raise ValueError(msg)
        if self.rrf_k <= 0:
            msg = "rrf_k must be > 0"
            raise ValueError(msg)
        if self.fusion_top_k <= 0:
            msg = "fusion_top_k must be > 0"
            raise ValueError(msg)


def get_rag_config() -> RagConfig:
    """从环境变量解析或使用默认 RAG 检索配置."""
    return RagConfig(
        vector_fetch_k=_read_rag_int(
            "RAG_VECTOR_FETCH_K", DEFAULT_RAG_VECTOR_FETCH_K,
        ),
        rerank_top_k=_read_rag_int("RERANK_TOP_K", DEFAULT_RERANK_TOP_K),
        multi_query_max_queries=_read_rag_int(
            "RAG_MULTI_QUERY_MAX_QUERIES", DEFAULT_RAG_MULTI_QUERY_MAX_QUERIES,
        ),
        rrf_k=_read_rag_int("RAG_RRF_K", DEFAULT_RAG_RRF_K),
        fusion_top_k=_read_rag_int("RAG_FUSION_TOP_K", DEFAULT_RAG_FUSION_TOP_K),
    )
