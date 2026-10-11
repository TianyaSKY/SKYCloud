"""云盘 RAG 对话：多查询向量召回、RRF 融合、Rerank，经 SSE 流式输出回答。"""

import asyncio
from contextlib import aclosing
import json
import os
from functools import lru_cache
from operator import itemgetter

from langchain_core.documents import Document
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableParallel, RunnableLambda
from langchain_openai import ChatOpenAI

from app.infra.extensions import SessionLocal
from app.infra.owned_work import run_owned
from app.features.chat import repository
from app.infra.llm.config import get_chat_model_config, get_embedding_model_config
from app.features.chat.query_rewrite import (
    build_multi_queries,
    build_retrieval_query,
    format_keyword_dimensions,
    require_keyword_dimensions,
    RewriteKeywordDimensions,
)
from app.features.chat.rerank import rerank_documents
from app.features.chat.retrieval_service import (
    RagRetrievalService, RetrievalSettings, RetrievalOperations, VectorSearch,
    fuse_docs_with_rrf as _fuse_docs_with_rrf,
)

from loguru import logger


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
    from app.infra.llm.client import TrackingOpenAIEmbeddings
    config = get_embedding_model_config()

    return TrackingOpenAIEmbeddings(
        api_key=config["key"],
        base_url=config["api"],
        model=config["model"],
    )


def _vector_search_docs(
        query_vector: list[float],
        query_text: str,
        workspace_id: int,
        limit: int,
        user_id: int,
) -> list[Document]:
    """按已生成的向量查询数据库；由调用方异步请求 embedding。"""
    return _db_search_by_vector(query_vector, query_text, workspace_id, limit, user_id)


def _db_search_by_vector(
        query_vector: list[float],
        query_text: str,
        workspace_id: int,
        limit: int,
        user_id: int,
) -> list[Document]:
    """用预计算向量查库；独立 session 保证线程池并发安全。"""
    results = repository.vector_rows(SessionLocal, query_vector, workspace_id, user_id, limit)

    docs: list[Document] = []
    for rank, row in enumerate(results, start=1):
        content = f"文件名: {row[2]}\n内容: {row[3]}"
        metadata = {
            "id": row[1],
            "retrieval_id": f"chunk:{row[0]}",
            "name": row[2],
            "mime_type": row[4],
            "chunk_index": row[5],
            "page_number": row[6],
            "distance": float(row[7]) if row[7] is not None else None,
            "rank": rank,
            "query_text": query_text,
        }
        docs.append(Document(page_content=content, metadata=metadata))
    return docs


def _retrieval_service(search: VectorSearch) -> RagRetrievalService:
    return RagRetrievalService(
        RetrievalSettings(RAG_VECTOR_FETCH_K, RAG_MULTI_QUERY_MAX_QUERIES, RAG_RRF_K, RAG_FUSION_TOP_K),
        RetrievalOperations(get_embeddings_model, search, rerank_documents,
                            build_multi_queries, build_retrieval_query, _fuse_docs_with_rrf),
    )


async def custom_db_retriever(query_text: str, workspace_id: int, *, user_id: int):
    """Single recall/rerank; the SQL port checks current membership."""
    return await _retrieval_service(_vector_search_docs).single_query(query_text, workspace_id, user_id=user_id)


async def multi_query_db_retriever(question: str, workspace_id: int, dimensions: RewriteKeywordDimensions,
                                 original_vector: list[float] | None = None, *, user_id: int):
    """Parallel recall/fusion/rerank, optionally reusing the original embedding."""
    return await _retrieval_service(_db_search_by_vector).multi_query(
        question, workspace_id, dimensions, original_vector, user_id=user_id)


def format_docs(docs):
    formatted = []
    for doc in docs:
        m = doc.metadata
        info = f"[文件: {m['name']} (ID: {m['id']})]\n{doc.page_content}"
        if m.get("page_number"):
            info = f"[文件: {m['name']} (ID: {m['id']}，第 {m['page_number']} 页)]\n{doc.page_content}"
        # 图片需提示模型用固定 Markdown 路径引用，前端才能渲染
        if m.get('mime_type', '').startswith('image/'):
            info += f"\n(这是一张图片，你可以使用 Markdown 语法展示它: ![图片名](/api/files/{m['id']}/download))"
        formatted.append(info)
    return "\n\n---\n\n".join(formatted)


def format_history(history):
    if isinstance(history, list):
        return "\n".join([f"{h.get('role', 'user')}: {h.get('content', '')}" for h in history])
    return str(history) if history else ""


async def embed_original_question(payload: dict) -> list[float]:
    """预计算原问题 embedding，与关键词改写并行以缩短首 token 延迟。"""
    question = str(payload.get("question", "") or "").strip()
    if not question:
        return []
    embeddings = get_embeddings_model()
    vector = await embeddings.aembed_query(question, user_id=int(payload.get("user_id") or 0))
    return vector[:1024]


async def retrieve_docs_with_rewrite(payload: dict):
    question = str(payload.get("question", "") or "")
    rewrite_output = payload.get("rewrite_output")
    original_vector = payload.get("original_vector") or None
    workspace_id = int(payload["workspace_id"])

    dimensions = require_keyword_dimensions(rewrite_output)
    return await multi_query_db_retriever(
        question, workspace_id, dimensions, original_vector=original_vector,
        user_id=int(payload.get("user_id") or 0),
    )


async def generate_chat_events(user_id: int, workspace_id: int, query: str, history: list):
    """SSE 异步生成器：关键词 → 检索状态 → 回答 token，并统一记 token 用量。"""
    from app.infra.llm.client import record_llm_usage

    llm = get_chat_model()
    formatted_history = format_history(history)

    # 关键词重写：多维度 JSON，便于下游拼接多路查询
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
                               "workspace_id": itemgetter("workspace_id"),
                               "user_id": itemgetter("user_id"),
                           } | RunnableLambda(retrieve_docs_with_rewrite).with_config(
                    {"run_name": "custom_db_retriever"}
                ) | format_docs,
                "question": itemgetter("question"),
                "history": itemgetter("history")
            })
            | answer_chain
    )

    # ---------------------------------------------------------------------------
    # Token 用量追踪
    # ---------------------------------------------------------------------------
    usage_accumulator: dict[str, int] = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }
    model_name_seen: str | None = None

    failed = False
    try:
        iterator = rag_chain.astream_events(
                {"question": query, "history": formatted_history, "user_id": user_id, "workspace_id": workspace_id},
                version="v2"
        )
        async with aclosing(iterator):
            async for event in iterator:
                kind = event["event"]

                # 仅在关键词链结束时推送完整关键词，避免中间态闪烁
                if kind == "on_chain_end" and event["name"] == "keyword_gen":
                    rewrite_output = event["data"].get("output")
                    dimensions = require_keyword_dimensions(rewrite_output)
                    keywords = format_keyword_dimensions(dimensions)
                    yield f"data: {json.dumps({'type': 'keywords', 'content': keywords})}\n\n"

                # 只转发 final_answer_model 的流，排除关键词生成阶段的 token
                elif kind == "on_chat_model_stream" and event["name"] == "final_answer_model":
                    content = event["data"]["chunk"].content
                    if content:
                        yield f"data: {json.dumps({'type': 'token', 'content': content})}\n\n"

                elif kind == "on_retriever_start" or (kind == "on_chain_start" and event["name"] == "custom_db_retriever"):
                    yield f"data: {json.dumps({'type': 'status', 'content': '正在检索相关文件...'})}\n\n"

                elif kind == "on_chain_end" and event["name"] == "custom_db_retriever":
                    # Expose only source metadata, never the complete retrieved
                    # chunks.  The new Assistant adapter persists this for the
                    # source panel while the legacy /api/chat client ignores it.
                    output = event.get("data", {}).get("output") or []
                    sources = []
                    if isinstance(output, list):
                        for doc in output:
                            metadata = getattr(doc, "metadata", {}) or {}
                            if not isinstance(metadata, dict):
                                continue
                            source = {
                                "file_id": metadata.get("id"),
                                "file_name": metadata.get("name"),
                                "page_number": metadata.get("page_number"),
                                "chunk_index": metadata.get("chunk_index"),
                            }
                            if source["file_id"] is not None:
                                sources.append(source)
                    if sources:
                        yield f"data: {json.dumps({'type': 'sources', 'sources': sources}, ensure_ascii=False)}\n\n"

                # 各 chat model 结束事件均可能带 usage_metadata，累加后统一落库
                elif kind == "on_chat_model_end":
                    output = event.get("data", {}).get("output")
                    if output and hasattr(output, "usage_metadata") and output.usage_metadata:
                        um = output.usage_metadata
                        usage_accumulator["prompt_tokens"] += um.get("input_tokens", 0)
                        usage_accumulator["completion_tokens"] += um.get("output_tokens", 0)
                        usage_accumulator["total_tokens"] += um.get("total_tokens", 0)
                    if not model_name_seen:
                        run_meta = event.get("metadata", {}) or {}
                        model_name_seen = run_meta.get("ls_model_name")

        if any(usage_accumulator.values()):
            yield f"data: {json.dumps({'type': 'usage', **usage_accumulator, 'model_name': model_name_seen})}\n\n"

    except Exception as e:
        logger.error("Chat request failed (error_type={})", type(e).__name__)
        yield f"data: {json.dumps({'type': 'error', 'payload': {'message': '对话请求失败，请稍后重试。'}})}\n\n"
    except BaseException:
        failed = True
        raise
    finally:
        # 成功或异常都记用量，避免漏计费
        try:
            await run_owned(
                record_llm_usage, cancel_queued=False,
                user_id=user_id,
                action="chat",
                model_name=model_name_seen,
                prompt_tokens=usage_accumulator["prompt_tokens"],
                completion_tokens=usage_accumulator["completion_tokens"],
                total_tokens=usage_accumulator["total_tokens"],
                query_summary=query,
            )
        except asyncio.CancelledError:
            if not failed:
                raise
