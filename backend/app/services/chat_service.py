import asyncio
import json
import logging
import os
from collections import defaultdict
from functools import lru_cache
from operator import itemgetter

from langchain_core.documents import Document
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableParallel, RunnableLambda
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from sqlalchemy import text

from app.extensions import SessionLocal
from app.services.model_config import get_chat_model_config, get_embedding_model_config
from app.services.query_rewrite import (
    build_multi_queries,
    build_retrieval_query,
    format_keyword_dimensions,
    require_keyword_dimensions,
    RewriteKeywordDimensions,
)
from app.services.rerank_service import rerank_documents

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


RAG_VECTOR_FETCH_K = _env_int("RAG_VECTOR_FETCH_K", 20)
RAG_MULTI_QUERY_MAX_QUERIES = _env_int("RAG_MULTI_QUERY_MAX_QUERIES", 3)
RAG_RRF_K = _env_int("RAG_RRF_K", 60)
RAG_FUSION_TOP_K = _env_int("RAG_FUSION_TOP_K", 30)


@lru_cache(maxsize=1)
def get_chat_model():
    config = get_chat_model_config()

    return ChatOpenAI(
        api_key=config["key"],
        base_url=config["api"],
        model=config["model"],
        temperature=0,
    )


@lru_cache(maxsize=1)
def get_embeddings_model():
    config = get_embedding_model_config()

    return OpenAIEmbeddings(
        api_key=config["key"],
        base_url=config["api"],
        model=config["model"],
    )


def _vector_search_docs(
        query_text: str,
        user_id: int,
        embeddings: OpenAIEmbeddings,
        limit: int,
) -> list[Document]:
    """单条查询的完整向量检索（embedding + DB），保留向后兼容。"""
    query_vector = embeddings.embed_query(query_text)[:1024]
    return _db_search_by_vector(query_vector, query_text, user_id, limit)


def _db_search_by_vector(
        query_vector: list[float],
        query_text: str,
        user_id: int,
        limit: int,
) -> list[Document]:
    """使用预计算的向量进行数据库搜索。使用独立 session 确保线程安全。"""
    sql = text("""
               SELECT id, name, description, mime_type, (vector_info <=> :vector) AS distance
                FROM files
                WHERE description IS NOT NULL
                  AND description != ''
           AND uploader_id = :user_id
                ORDER BY vector_info <=> :vector
                   LIMIT :limit
                """)

    session = SessionLocal()
    try:
        results = session.execute(
            sql,
            {"vector": str(query_vector), "user_id": user_id, "limit": limit},
        ).fetchall()
    finally:
        session.close()

    docs: list[Document] = []
    for rank, row in enumerate(results, start=1):
        content = f"文件名: {row[1]}\n描述: {row[2]}"
        metadata = {
            "id": row[0],
            "name": row[1],
            "mime_type": row[3],
            "distance": float(row[4]) if row[4] is not None else None,
            "rank": rank,
            "query_text": query_text,
        }
        docs.append(Document(page_content=content, metadata=metadata))
    return docs


def _fuse_docs_with_rrf(
        result_sets: list[list[Document]],
        rrf_k: int,
        top_k: int,
) -> list[Document]:
    if not result_sets:
        return []

    scores: dict[int, float] = defaultdict(float)
    doc_map: dict[int, Document] = {}

    for docs in result_sets:
        for rank, doc in enumerate(docs, start=1):
            doc_id = int(doc.metadata["id"])
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


async def custom_db_retriever(query_text: str, user_id: int):
    """单查询向量检索，保留兼容。"""
    embeddings = get_embeddings_model()
    docs = _vector_search_docs(
        query_text, user_id, embeddings, RAG_VECTOR_FETCH_K)
    docs = await rerank_documents(query_text, docs)
    logger.info(f"单查询检索结果: {len(docs)}")
    return docs


async def multi_query_db_retriever(
        question: str,
        user_id: int,
        dimensions: RewriteKeywordDimensions,
        original_vector: list[float] | None = None,
):
    import time
    t0 = time.perf_counter()

    queries = build_multi_queries(
        question,
        dimensions,
        max_queries=RAG_MULTI_QUERY_MAX_QUERIES,
    )
    if not queries and question.strip():
        queries = [question.strip()]

    embeddings = get_embeddings_model()
    loop = asyncio.get_running_loop()

    # ---- 阶段 1: embedding ----
    # 如果已预计算原始问题向量，只需 embed 额外查询
    t1 = time.perf_counter()
    if original_vector and queries:
        additional_queries = queries[1:]  # queries[0] 始终是原始问题
        if additional_queries:
            extra_vectors = await loop.run_in_executor(
                None, embeddings.embed_documents, additional_queries
            )
            all_vectors = [original_vector] + [v[:1024] for v in extra_vectors]
        else:
            all_vectors = [original_vector]
        t2 = time.perf_counter()
        logger.info(
            f"[计时] 增量 embedding {len(additional_queries)} 条: {t2 - t1:.2f}s"
            f" (原始问题已并行预计算)")
    else:
        all_vectors = await loop.run_in_executor(
            None, embeddings.embed_documents, queries
        )
        all_vectors = [v[:1024] for v in all_vectors]
        t2 = time.perf_counter()
        logger.info(f"[计时] 批量 embedding {len(queries)} 条: {t2 - t1:.2f}s")

    # ---- 阶段 2: 顺序数据库向量检索 ----
    result_sets: list[list[Document]] = []
    for query_text, vector in zip(queries, all_vectors):
        try:
            docs = _db_search_by_vector(
                vector, query_text, user_id, RAG_VECTOR_FETCH_K)
            if docs:
                result_sets.append(docs)
        except Exception as exc:
            logger.warning(
                f"Multi-query recall failed for query='{query_text}': {exc}")
    t3 = time.perf_counter()
    logger.info(f"[计时] DB 向量检索 {len(queries)} 条: {t3 - t2:.2f}s")

    # ---- 阶段 3: RRF 融合 ----
    fused_docs = _fuse_docs_with_rrf(
        result_sets, rrf_k=RAG_RRF_K, top_k=RAG_FUSION_TOP_K)

    # ---- 阶段 4: Rerank ----
    retrieval_query = build_retrieval_query(question, dimensions)
    reranked_docs = await rerank_documents(retrieval_query, fused_docs)
    t4 = time.perf_counter()
    logger.info(f"[计时] Rerank: {t4 - t3:.2f}s")

    logger.info(
        f"多查询融合检索完成: queries={len(queries)}, fused={len(fused_docs)}, "
        f"reranked={len(reranked_docs)}, 总耗时={t4 - t0:.2f}s"
    )
    return reranked_docs


def format_docs(docs):
    formatted = []
    for doc in docs:
        m = doc.metadata
        info = f"[文件: {m['name']} (ID: {m['id']})]\n{doc.page_content}"
        # 如果是图片，提示 AI 可以使用特定语法引用
        if m.get('mime_type', '').startswith('image/'):
            info += f"\n(这是一张图片，你可以使用 Markdown 语法展示它: ![图片名](/api/files/{m['id']}/download))"
        formatted.append(info)
    return "\n\n---\n\n".join(formatted)


def format_history(history):
    if isinstance(history, list):
        return "\n".join([f"{h.get('role', 'user')}: {h.get('content', '')}" for h in history])
    return str(history) if history else ""


async def embed_original_question(payload: dict) -> list[float]:
    """预计算原始问题的 embedding，与关键词改写并行执行。"""
    question = str(payload.get("question", "") or "").strip()
    if not question:
        return []
    embeddings = get_embeddings_model()
    loop = asyncio.get_running_loop()
    vector = await loop.run_in_executor(None, embeddings.embed_query, question)
    return vector[:1024]


async def retrieve_docs_with_rewrite(payload: dict):
    question = str(payload.get("question", "") or "")
    rewrite_output = payload.get("rewrite_output")
    original_vector = payload.get("original_vector") or None
    current_user_id = int(payload["user_id"])

    dimensions = require_keyword_dimensions(rewrite_output)
    return await multi_query_db_retriever(
        question, current_user_id, dimensions, original_vector=original_vector
    )


async def generate_chat_events(user_id, query: str, history: list):
    """异步生成器，用于 SSE 流式输出"""
    llm = get_chat_model()
    formatted_history = format_history(history)

    # 关键词重写链：多维度提取，统一输出 JSON，便于下游检索拼接
    rewrite_prompt = ChatPromptTemplate.from_template("""
请从用户问题中提取检索关键词。
输出要求：
1. 主题词（topic_terms）
2. 实体词（entity_terms）
3. 时间词（time_terms）
4. 文件类型词（file_type_terms）
5. 动作词（action_terms）
6. 同义扩展词（synonym_terms）

要求：
1. 使用与用户问题相同的语言输出关键词（中文问题输出中文，英文问题输出英文）。
2. 在 synonym_terms 中可补充跨语言同义词以提高召回率。
3. 无内容时返回空数组。
4. 不要编造无关词。

问题：{question}
""")
    rewrite_llm = llm.with_structured_output(
        RewriteKeywordDimensions, strict=False)
    rewriter = (rewrite_prompt | rewrite_llm).with_config(
        {"run_name": "keyword_gen"})

    # 最终回答的提示词模板：使用中文提示词
    answer_prompt = ChatPromptTemplate.from_template("""
你是一个云盘助手。请根据以下参考信息回答用户问题。

### 指令要求：
1. 请使用中文回答。
2. **严禁在回答开头输出搜索关键词。** 直接开始你的回答。
3. **展示图片：** 如果参考信息中有图片且相关，必须严格按照以下格式展示：
   `![图片描述](/api/files/文件ID/download)`
   注意：文件ID 必须替换为参考信息中提供的 ID。
4. 保持回答简洁专业。

### 历史对话：
{history}

### 参考信息：
{context}

### 问题：
{question}
""")

    # 最终回答链
    answer_chain = (
        answer_prompt |
        llm.with_config({"run_name": "final_answer_model", "tags": ["final_answer"]}) |
        StrOutputParser()
    ).with_config({"run_name": "final_answer_chain"})

    rag_chain = (
        RunnableParallel({
            "context": {
                "rewrite_output": rewriter,
                "original_vector": RunnableLambda(
                    embed_original_question
                ).with_config({"run_name": "embed_original"}),
                "question": itemgetter("question"),
                "user_id": itemgetter("user_id"),
            } | RunnableLambda(retrieve_docs_with_rewrite).with_config(
                {"run_name": "custom_db_retriever"}
            ) | format_docs,
            "question": itemgetter("question"),
            "history": itemgetter("history")
        })
        | answer_chain
    )

    try:
        async for event in rag_chain.astream_events(
                {"question": query, "history": formatted_history, "user_id": user_id},
                version="v2"
        ):
            kind = event["event"]

            # 处理关键词生成（仅在链结束时发送完整关键词）
            if kind == "on_chain_end" and event["name"] == "keyword_gen":
                rewrite_output = event["data"].get("output")
                dimensions = require_keyword_dimensions(rewrite_output)
                keywords = format_keyword_dimensions(dimensions)
                yield f"data: {json.dumps({'type': 'keywords', 'content': keywords})}\n\n"

            # 处理最终回答的流：通过 run_name 'final_answer_model' 确保不包含关键词生成的 token
            elif kind == "on_chat_model_stream" and event["name"] == "final_answer_model":
                content = event["data"]["chunk"].content
                if content:
                    yield f"data: {json.dumps({'type': 'token', 'content': content})}\n\n"

            # 处理状态
            elif kind == "on_retriever_start" or (kind == "on_chain_start" and event["name"] == "custom_db_retriever"):
                yield f"data: {json.dumps({'type': 'status', 'content': '🔍 正在检索相关文件...'})}\n\n"

    except Exception as e:
        logger.error(f"Chat error: {e}", exc_info=True)
        yield f"data: {json.dumps({'type': 'status', 'content': f'❌ 错误: {str(e)}'})}\n\n"
