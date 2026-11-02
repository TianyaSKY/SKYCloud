"""云盘 RAG 对话: 多查询向量召回、RRF 融合、Rerank, 经 SSE 流式输出回答."""

from __future__ import annotations

import os
from contextlib import aclosing
from functools import lru_cache
from operator import itemgetter
from typing import TYPE_CHECKING, Any, Protocol, TypedDict, cast

from langchain_core.documents import Document
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable, RunnableLambda, RunnableParallel
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, SecretStr

from app.features.chat import repository
from app.features.chat.query_rewrite import (
    RewriteKeywordDimensions,
    build_multi_queries,
    build_retrieval_query,
    require_keyword_dimensions,
)
from app.features.chat.rerank import rerank_documents
from app.features.chat.retrieval_service import (
    RagRetrievalService,
    RetrievalOperations,
    RetrievalSettings,
    VectorSearch,
)
from app.features.chat.retrieval_service import (
    fuse_docs_with_rrf as _fuse_docs_with_rrf,
)
from app.features.chat.stream_events import stream_chat_events
from app.infra.extensions import SessionLocal
from app.infra.llm import client as llm_client
from app.infra.llm.config import get_chat_model_config, get_embedding_model_config
from app.infra.owned_work import closeable_event_stream

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    from app.features.assistant.event_protocol import AssistantEvent
    from app.infra.llm.usage import TokenUsage


class _StructuredChat(Protocol):
    """当前流水线使用的类型化 SDK 结构化输出入口协议."""

    def with_structured_output(
        self, schema: type[BaseModel], *, strict: bool,
    ) -> Runnable[Any, Any]:
        """将传入的 Pydantic 模型绑定至结构化可运行对象."""
        ...


class _QuestionPayload(TypedDict, total=False):
    """并行改写分支传递给向量嵌入器的输入结构."""

    question: str
    user_id: int


class _RetrievalPayload(TypedDict):
    """多查询检索分支聚合处理的输入结构."""

    question: str
    workspace_id: int
    rewrite_output: object
    original_vector: list[float] | None
    user_id: int


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


RAG_VECTOR_FETCH_K = _env_int("RAG_VECTOR_FETCH_K", 20)
RAG_MULTI_QUERY_MAX_QUERIES = _env_int("RAG_MULTI_QUERY_MAX_QUERIES", 3)
RAG_RRF_K = _env_int("RAG_RRF_K", 60)
RAG_FUSION_TOP_K = _env_int("RAG_FUSION_TOP_K", 30)


@lru_cache(maxsize=1)
def get_chat_model() -> ChatOpenAI:
    """返回用于改写和问答的已缓存聊天模型客户端."""
    config = get_chat_model_config()

    return ChatOpenAI(
        api_key=SecretStr(config["key"]),
        base_url=config["api"],
        model=config["model"],
        temperature=0,
    )


@lru_cache(maxsize=1)
def get_embeddings_model() -> llm_client.TrackingOpenAIEmbeddings:
    """返回记录 Token 用量的已缓存嵌入模型客户端."""
    config = get_embedding_model_config()

    return llm_client.TrackingOpenAIEmbeddings(
        api_key=SecretStr(config["key"]),
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
    """按已生成的向量查询数据库; 由调用方异步请求 embedding."""
    return _db_search_by_vector(query_vector, query_text, workspace_id, limit, user_id)


def _db_search_by_vector(
        query_vector: list[float],
        query_text: str,
        workspace_id: int,
        limit: int,
        user_id: int,
) -> list[Document]:
    """用预计算向量查库; 独立 session 保证线程池并发安全."""
    results = repository.vector_rows(
        SessionLocal, query_vector, workspace_id, user_id, limit,
    )

    docs: list[Document] = []
    for rank, row in enumerate(results, start=1):
        content = f"文件名: {row[2]}\n内容: {row[3]}"
        metadata: dict[str, Any] = {
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
        RetrievalSettings(
            RAG_VECTOR_FETCH_K, RAG_MULTI_QUERY_MAX_QUERIES,
            RAG_RRF_K, RAG_FUSION_TOP_K,
        ),
        RetrievalOperations(
            get_embeddings_model, search, rerank_documents,
            build_multi_queries, build_retrieval_query, _fuse_docs_with_rrf,
        ),
    )


async def custom_db_retriever(
    query_text: str, workspace_id: int, *, user_id: int,
) -> list[Document]:
    """单路召回与重排; SQL 端口检查当前成员资格有效性."""
    service = _retrieval_service(_vector_search_docs)
    return await service.single_query(query_text, workspace_id, user_id=user_id)


async def multi_query_db_retriever(
    question: str,
    workspace_id: int,
    dimensions: RewriteKeywordDimensions,
    original_vector: list[float] | None = None,
    *,
    user_id: int,
) -> list[Document]:
    """并行召回、融合与重排, 可选复用原始嵌入向量."""
    service = _retrieval_service(_db_search_by_vector)
    return await service.multi_query(
        question, workspace_id, dimensions, original_vector, user_id=user_id,
    )


def format_docs(docs: list[Document]) -> str:
    """为最终问答提示词渲染检索出的分块内容."""
    formatted: list[str] = []
    for doc in docs:
        m = doc.metadata
        info = f"[文件: {m['name']} (ID: {m['id']})]\n{doc.page_content}"
        if m.get("page_number"):
            info = (
                f"[文件: {m['name']} (ID: {m['id']}, 第 {m['page_number']} 页)]"
                f"\n{doc.page_content}"
            )
        # 图片需提示模型用固定 Markdown 路径引用, 前端才能渲染
        if m.get("mime_type", "").startswith("image/"):
            info += (
                "\n(这是一张图片, 你可以使用 Markdown 语法展示它: "
                f"![图片名](/api/files/{m['id']}/download))"
            )
        formatted.append(info)
    return "\n\n---\n\n".join(formatted)


def format_history(history: list[dict[str, Any]] | str | None) -> str:
    """渲染对话历史以便插值到提示词模板中."""
    if isinstance(history, list):
        return "\n".join([
            f"{h.get('role', 'user')}: {h.get('content', '')}" for h in history
        ])
    return str(history) if history else ""


async def embed_original_question(payload: _QuestionPayload) -> list[float]:
    """预计算原问题 embedding, 与关键词改写并行以缩短首 token 延迟."""
    question = str(payload.get("question", "") or "").strip()
    if not question:
        return []
    embeddings = get_embeddings_model()
    vector = await embeddings.aembed_query(
        question, user_id=int(payload.get("user_id") or 0),
    )
    return vector[:1024]


async def retrieve_docs_with_rewrite(payload: _RetrievalPayload) -> list[Document]:
    """针对改写后的关键词维度执行多查询检索流水线."""
    question = str(payload.get("question", "") or "")
    rewrite_output = payload.get("rewrite_output")
    original_vector = payload.get("original_vector") or None
    workspace_id = int(payload["workspace_id"])

    dimensions = require_keyword_dimensions(rewrite_output)
    return await multi_query_db_retriever(
        question, workspace_id, dimensions, original_vector=original_vector,
        user_id=int(payload.get("user_id") or 0),
    )


def _record_chat_usage(usage: TokenUsage) -> None:
    """通过共享的 LLM 记账入口持久化一条对话用量记录."""
    llm_client.record_llm_usage(usage=usage)


async def generate_chat_events(
    user_id: int, workspace_id: int, query: str, history: list[dict[str, str]],
) -> AsyncGenerator[AssistantEvent, None]:
    """生成结构化 RAG 事件流并在结束时持久化确认的用量."""
    llm = get_chat_model()
    formatted_history = format_history(history)

    # 关键词重写: 多维度 JSON, 便于下游拼接多路查询
    rewrite_prompt = ChatPromptTemplate.from_template("""
请从用户问题中提取检索关键词。
输出要求:
1. 主题词(topic_terms)
2. 实体词(entity_terms)
3. 时间词(time_terms)
4. 文件类型词(file_type_terms)
5. 动作词(action_terms)
6. 同义扩展词(synonym_terms)

要求:
1. 使用与用户问题相同的语言输出关键词(中文问题输出中文, 英文问题输出英文)。
2. 在 synonym_terms 中可补充跨语言同义词以提高召回率。
3. 无内容时返回空数组。
4. 不要编造无关词。

问题:{question}
""")
    rewrite_llm = cast("_StructuredChat", llm).with_structured_output(
        RewriteKeywordDimensions, strict=False)
    rewriter: Runnable[Any, Any] = (rewrite_prompt | rewrite_llm).with_config(
        {"run_name": "keyword_gen"})

    answer_prompt = ChatPromptTemplate.from_template("""
你是一个云盘助手。请根据以下参考信息回答用户问题。

### 指令要求:
1. 请使用中文回答。
2. **严禁在回答开头输出搜索关键词。** 直接开始你的回答。
3. **展示图片:** 如果参考信息中有图片且相关, 必须严格按照以下格式展示:
   `![图片描述](/api/files/文件ID/download)`
   注意: 文件ID 必须替换为参考信息中提供的 ID。
4. 保持回答简洁专业。

### 历史对话:
{history}

### 参考信息:
{context}

### 问题:
{question}
""")

    answer_chain: Runnable[Any, str] = (
            answer_prompt |
            llm.with_config(
                {"run_name": "final_answer_model", "tags": ["final_answer"]},
            ) |
            StrOutputParser()
    ).with_config({"run_name": "final_answer_chain"})

    rag_steps: dict[str, Runnable[Any, Any] | Callable[[Any], Any]] = {
        "context": {
                       "rewrite_output": rewriter,
                       "original_vector": RunnableLambda(
                           embed_original_question,
                       ).with_config({"run_name": "embed_original"}),
                       "question": itemgetter("question"),
                       "workspace_id": itemgetter("workspace_id"),
                       "user_id": itemgetter("user_id"),
                   } | RunnableLambda(retrieve_docs_with_rewrite).with_config(
            {"run_name": "custom_db_retriever"},
        ) | format_docs,
        "question": itemgetter("question"),
        "history": itemgetter("history"),
    }

    rag_chain: Runnable[Any, str] = (
            RunnableParallel(rag_steps)
            | answer_chain
    )

    events = stream_chat_events(
        lambda: closeable_event_stream(rag_chain.astream_events(
            {
                "question": query,
                "history": formatted_history,
                "user_id": user_id,
                "workspace_id": workspace_id,
            },
            version="v2",
        )),
        user_id=user_id, query=query, record_usage=_record_chat_usage,
    )
    async with aclosing(events):
        async for event in events:
            yield event
