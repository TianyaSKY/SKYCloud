"""RAG vector recall, fusion and reranking behind explicit resource contracts.

The SQL search port returns detached Documents and owns its Session. Every
started search drains before cancellation returns; request identity is passed
per call and is never stored in a cached embeddings client.
"""
import asyncio
from collections import defaultdict
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

from langchain_core.documents import Document

from app.exceptions import PermissionDeniedError, ServiceOperationError
from app.features.chat.query_rewrite import RewriteKeywordDimensions
from app.infra.owned_work import run_owned
from loguru import logger


class EmbeddingsClient(Protocol):
    async def aembed_query(self, text: str, *, user_id: int) -> list[float]: ...
    async def aembed_documents(self, texts: list[str], *, user_id: int) -> list[list[float]]: ...


VectorSearch = Callable[[list[float], str, int, int, int], list[Document]]


class QueryBuilder(Protocol):
    def __call__(self, question: str, dimensions: RewriteKeywordDimensions,
                 *, max_queries: int) -> list[str]: ...


class DocumentFusion(Protocol):
    def __call__(self, result_sets: list[list[Document]],
                 *, rrf_k: int, top_k: int) -> list[Document]: ...


@dataclass(frozen=True)
class RetrievalSettings:
    vector_fetch_k: int
    max_queries: int
    rrf_k: int
    fusion_top_k: int


@dataclass(frozen=True)
class RetrievalOperations:
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
    """RRF 融合多路召回；同分时按向量 distance 打破平局。"""
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
    def __init__(self, settings: RetrievalSettings, operations: RetrievalOperations):
        self.settings = settings
        self.operations = operations

    async def single_query(self, query_text: str, workspace_id: int, *, user_id: int) -> list[Document]:
        """单查询向量检索 + rerank；SQL 边界核验当前成员资格。"""
        embeddings = self.operations.embeddings()
        query_vector = (await embeddings.aembed_query(query_text, user_id=user_id))[:1024]
        docs = await run_owned(
            self.operations.search,
            query_vector,
            query_text,
            workspace_id,
            self.settings.vector_fetch_k,
            user_id,
        )
        docs = await self.operations.rerank(query_text, docs)
        logger.info(f"单查询检索结果: {len(docs)}")
        return docs

    async def multi_query(
            self,
            question: str,
            workspace_id: int,
            dimensions: RewriteKeywordDimensions,
            original_vector: list[float] | None = None,
            *, user_id: int,
    ) -> list[Document]:
        """多查询并行召回 → RRF → Rerank。

        original_vector 可与关键词改写并行预计算，避免重复 embed 原问题。
        """
        import time
        t0 = time.perf_counter()

        queries = self.operations.build_queries(
            question,
            dimensions,
            max_queries=self.settings.max_queries,
        )
        if not queries and question.strip():
            queries = [question.strip()]

        embeddings = self.operations.embeddings()

        # ---------------------------------------------------------------------------
        # 阶段 1: embedding
        # ---------------------------------------------------------------------------
        # 若已预计算原问题向量，只 embed 额外查询
        t1 = time.perf_counter()
        if original_vector and queries:
            additional_queries = queries[1:]  # queries[0] 始终是原始问题
            if additional_queries:
                extra_vectors = await embeddings.aembed_documents(additional_queries, user_id=user_id)
                all_vectors = [original_vector] + [v[:1024] for v in extra_vectors]
            else:
                all_vectors = [original_vector]
            t2 = time.perf_counter()
            logger.info(
                f"[计时] 增量 embedding {len(additional_queries)} 条: {t2 - t1:.2f}s"
                f" (原始问题已并行预计算)")
        else:
            all_vectors = await embeddings.aembed_documents(queries, user_id=user_id)
            all_vectors = [v[:1024] for v in all_vectors]
            t2 = time.perf_counter()
            logger.info(f"[计时] 批量 embedding {len(queries)} 条: {t2 - t1:.2f}s")

        if len(all_vectors) != len(queries):
            raise ServiceOperationError("Embedding response does not match retrieval queries")

        # ---------------------------------------------------------------------------
        # 阶段 2: 并行数据库向量检索
        # ---------------------------------------------------------------------------
        async def _search_one(q_text: str, vec: list[float]):
            try:
                return await run_owned(
                    self.operations.search, vec, q_text, workspace_id, self.settings.vector_fetch_k, user_id
                )
            except Exception as exc:
                # Keep partial recall on ordinary failures. Cancellation still
                # propagates, and TaskGroup waits for every owned worker to drain.
                return exc

        async with asyncio.TaskGroup() as group:
            search_tasks = [group.create_task(_search_one(qt, v)) for qt, v in zip(queries, all_vectors)]
        raw_results = [task.result() for task in search_tasks]

        for result in raw_results:
            if isinstance(result, PermissionDeniedError):
                raise result
        result_sets: list[list[Document]] = []
        for res in raw_results:
            if isinstance(res, Exception):
                logger.warning("Multi-query recall failed (error_type={})", type(res).__name__)
            elif res:
                result_sets.append(res)
        if raw_results and all(isinstance(result, Exception) for result in raw_results):
            raise ServiceOperationError("File retrieval unavailable")
        t3 = time.perf_counter()
        logger.info(f"[计时] DB 向量检索 {len(queries)} 条 (并行): {t3 - t2:.2f}s")

        # ---------------------------------------------------------------------------
        # 阶段 3: RRF 融合
        # ---------------------------------------------------------------------------
        fused_docs = self.operations.fuse(
            result_sets, rrf_k=self.settings.rrf_k, top_k=self.settings.fusion_top_k)

        # ---------------------------------------------------------------------------
        # 阶段 4: Rerank
        # ---------------------------------------------------------------------------
        retrieval_query = self.operations.build_query(question, dimensions)
        reranked_docs = await self.operations.rerank(retrieval_query, fused_docs)
        t4 = time.perf_counter()
        logger.info(f"[计时] Rerank: {t4 - t3:.2f}s")

        logger.info(
            f"多查询融合检索完成: queries={len(queries)}, fused={len(fused_docs)}, "
            f"reranked={len(reranked_docs)}, 总耗时={t4 - t0:.2f}s"
        )
        return reranked_docs
