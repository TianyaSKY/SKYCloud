"""RAG vector recall, fusion and reranking behind explicit resource contracts.

The SQL search port returns detached Documents and owns its Session. Every
started search drains before cancellation returns; request identity is passed
per call and is never stored in a cached embeddings client.
"""

import asyncio
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from langchain_core.documents import Document
from loguru import logger

from app.exceptions import PermissionDeniedError, ServiceOperationError
from app.features.chat.query_rewrite import RewriteKeywordDimensions
from app.infra.owned_work import run_owned


class EmbeddingsClient(Protocol):
    """Embedding client that accepts request identity as a keyword argument."""

    async def aembed_query(
        self, text: str, **kwargs: str | float | None,
    ) -> list[float]:
        """Embed one query text."""
        ...

    async def aembed_documents(
        self,
        texts: list[str],
        chunk_size: int | None = None,
        **kwargs: str | float | None,
    ) -> list[list[float]]:
        """Embed a batch of document texts."""
        ...


VectorSearch = Callable[[list[float], str, int, int, int], list[Document]]


class QueryBuilder(Protocol):
    """Build the multi-query variants used for parallel recall."""

    def __call__(self, question: str, dimensions: RewriteKeywordDimensions,
                 *, max_queries: int) -> list[str]:
        """Return ordered, de-duplicated retrieval queries."""
        ...


class DocumentFusion(Protocol):
    """Fuse ranked document lists into one ordering."""

    def __call__(self, result_sets: list[list[Document]],
                 *, rrf_k: int, top_k: int) -> list[Document]:
        """Return the fused document ranking."""
        ...


@dataclass(frozen=True)
class RetrievalSettings:
    """Recall depth, fusion width and rerank policy for one deployment."""

    vector_fetch_k: int
    max_queries: int
    rrf_k: int
    fusion_top_k: int


@dataclass(frozen=True)
class RetrievalOperations:
    """External ports the retrieval service calls."""

    embeddings: Callable[[], EmbeddingsClient]
    search: VectorSearch
    rerank: Callable[[str, list[Document]], Awaitable[list[Document]]]
    build_queries: QueryBuilder
    build_query: Callable[[str, RewriteKeywordDimensions], str]
    fuse: DocumentFusion


def fuse_docs_with_rrf(
        result_sets: list[list[Document]],
        rrf_k: int,
        top_k: int,
) -> list[Document]:
    """RRF 融合多路召回; 同分时按向量 distance 打破平局."""
    if not result_sets:
        return []

    scores: dict[str | int, float] = defaultdict(float)
    doc_map: dict[str | int, Document] = {}

    for docs in result_sets:
        for rank, doc in enumerate(docs, start=1):
            doc_id = doc.metadata.get("retrieval_id", doc.metadata["id"])
            scores[doc_id] += 1.0 / (rrf_k + rank)
            if doc_id not in doc_map:
                doc_map[doc_id] = doc

    ranked_ids = sorted(
        scores.keys(),
        key=lambda doc_id: (-scores[doc_id],
                            doc_map[doc_id].metadata.get("distance", float("inf"))),
    )
    fused_docs: list[Document] = []
    for doc_id in ranked_ids[:top_k]:
        doc = doc_map[doc_id]
        doc.metadata["rrf_score"] = scores[doc_id]
        fused_docs.append(doc)
    return fused_docs


class RagRetrievalService:
    """Single- and multi-query recall with fusion and reranking."""

    def __init__(
        self, settings: RetrievalSettings, operations: RetrievalOperations,
    ) -> None:
        """Bind retrieval policy to its embedding, SQL and rerank ports."""
        self.settings = settings
        self.operations = operations

    async def single_query(
        self, query_text: str, workspace_id: int, *, user_id: int,
    ) -> list[Document]:
        """单查询向量检索 + rerank; SQL 边界核验当前成员资格."""
        embeddings = self.operations.embeddings()
        query_vector = (
            await embeddings.aembed_query(query_text, user_id=user_id)
        )[:1024]
        docs = await run_owned(
            self.operations.search,
            query_vector,
            query_text,
            workspace_id,
            self.settings.vector_fetch_k,
            user_id,
        )
        docs = await self.operations.rerank(query_text, docs)
        logger.info("单查询检索结果: {}", len(docs))
        return docs

    async def multi_query(
            self,
            question: str,
            workspace_id: int,
            dimensions: RewriteKeywordDimensions,
            original_vector: list[float] | None = None,
            *, user_id: int,
    ) -> list[Document]:
        """多查询并行召回 -> RRF -> Rerank.

        original_vector 可与关键词改写并行预计算, 避免重复 embed 原问题.
        """
        t0 = time.perf_counter()

        queries = self._plan_queries(question, dimensions)
        embeddings = self.operations.embeddings()
        all_vectors = await self._embed_queries(
            embeddings, queries, original_vector, user_id,
        )

        result_sets, searched_at = await self._search_all(
            queries, all_vectors, workspace_id, user_id,
        )

        fused_docs = self.operations.fuse(
            result_sets, rrf_k=self.settings.rrf_k, top_k=self.settings.fusion_top_k)

        retrieval_query = self.operations.build_query(question, dimensions)
        reranked_docs = await self.operations.rerank(retrieval_query, fused_docs)
        t4 = time.perf_counter()
        logger.info("[计时] Rerank: {:.2f}s", t4 - searched_at)

        logger.info(
            "多查询融合检索完成: queries={}, fused={}, reranked={}, 总耗时={:.2f}s",
            len(queries), len(fused_docs), len(reranked_docs), t4 - t0,
        )
        return reranked_docs

    def _plan_queries(
        self, question: str, dimensions: RewriteKeywordDimensions,
    ) -> list[str]:
        """Build query variants, falling back to the trimmed question."""
        queries = self.operations.build_queries(
            question,
            dimensions,
            max_queries=self.settings.max_queries,
        )
        if not queries and question.strip():
            queries = [question.strip()]
        return queries

    async def _embed_queries(
        self,
        embeddings: EmbeddingsClient,
        queries: list[str],
        original_vector: list[float] | None,
        user_id: int,
    ) -> list[list[float]]:
        """Embed query variants, reusing the precomputed original vector."""
        t1 = time.perf_counter()
        if original_vector and queries:
            additional_queries = queries[1:]  # queries[0] 始终是原始问题
            if additional_queries:
                extra_vectors = await embeddings.aembed_documents(
                    additional_queries, user_id=user_id)
                all_vectors = [original_vector] + [v[:1024] for v in extra_vectors]
            else:
                all_vectors = [original_vector]
            t2 = time.perf_counter()
            logger.info(
                "[计时] 增量 embedding {} 条: {:.2f}s (原始问题已并行预计算)",
                len(additional_queries), t2 - t1)
        else:
            embedded = await embeddings.aembed_documents(queries, user_id=user_id)
            all_vectors = [v[:1024] for v in embedded]
            t2 = time.perf_counter()
            logger.info(
                "[计时] 批量 embedding {} 条: {:.2f}s", len(queries), t2 - t1)

        if len(all_vectors) != len(queries):
            message = "Embedding response does not match retrieval queries"
            raise ServiceOperationError(message)
        return all_vectors

    async def _search_all(
        self,
        queries: list[str],
        all_vectors: list[list[float]],
        workspace_id: int,
        user_id: int,
    ) -> tuple[list[list[Document]], float]:
        """Run every vector search concurrently and keep partial recall."""
        t2 = time.perf_counter()

        async def _search_one(
            query_text: str, vector: list[float],
        ) -> list[Document] | Exception:
            try:
                return await run_owned(
                    self.operations.search, vector, query_text, workspace_id,
                    self.settings.vector_fetch_k, user_id,
                )
            except Exception as exc:
                # Keep partial recall on ordinary failures. Cancellation still
                # propagates, and TaskGroup waits for every owned worker to drain.
                return exc

        async with asyncio.TaskGroup() as group:
            search_tasks = [
                group.create_task(_search_one(query_text, vector))
                for query_text, vector in zip(queries, all_vectors, strict=False)
            ]
        raw_results = [task.result() for task in search_tasks]

        for result in raw_results:
            if isinstance(result, PermissionDeniedError):
                raise result
        result_sets: list[list[Document]] = []
        for result in raw_results:
            if isinstance(result, Exception):
                logger.warning(
                    "Multi-query recall failed (error_type={})",
                    type(result).__name__,
                )
            elif result:
                result_sets.append(result)
        if raw_results and all(
                isinstance(result, Exception) for result in raw_results):
            message = "File retrieval unavailable"
            raise ServiceOperationError(message)
        t3 = time.perf_counter()
        logger.info(
            "[计时] DB 向量检索 {} 条 (并行): {:.2f}s", len(queries), t3 - t2)
        return result_sets, t3
