import json
import logging
import os
from collections import defaultdict
from operator import itemgetter

from langchain_core.documents import Document
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableParallel, RunnableLambda
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from sqlalchemy import text

from app.extensions import db
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
RAG_MULTI_QUERY_MAX_QUERIES = _env_int("RAG_MULTI_QUERY_MAX_QUERIES", 6)
RAG_RRF_K = _env_int("RAG_RRF_K", 60)
RAG_FUSION_TOP_K = _env_int("RAG_FUSION_TOP_K", 30)


def get_chat_model():
    config = get_chat_model_config()

    return ChatOpenAI(
        api_key=config["key"],
        base_url=config["api"],
        model=config["model"],
        temperature=0,
    )


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
    query_vector = embeddings.embed_query(query_text)[:1024]

    sql = text("""
               SELECT id, name, description, mime_type, (vector_info <=> :vector) AS distance
                FROM files
                WHERE description IS NOT NULL
                  AND description != ''
           AND uploader_id = :user_id
                ORDER BY vector_info <=> :vector
                   LIMIT :limit
               """)

    results = db.session.execute(
        sql,
        {"vector": str(query_vector), "user_id": user_id, "limit": RAG_VECTOR_FETCH_K},
    ).fetchall()
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
        key=lambda doc_id: (-scores[doc_id], doc_map[doc_id].metadata.get("distance", float("inf"))),
    )
    fused_docs: list[Document] = []
    for doc_id in ranked_ids[:top_k]:
        doc = doc_map[doc_id]
        doc.metadata["rrf_score"] = scores[doc_id]
        fused_docs.append(doc)
    return fused_docs


def custom_db_retriever(query_text: str, user_id: int):
    """单查询向量检索，保留兼容。"""
    embeddings = get_embeddings_model()
    docs = _vector_search_docs(query_text, user_id, embeddings, RAG_VECTOR_FETCH_K)
    docs = rerank_documents(query_text, docs)
    logger.info(f"单查询检索结果: {len(docs)}")
    return docs


def multi_query_db_retriever(
        question: str,
        user_id: int,
        dimensions: RewriteKeywordDimensions,
):
    queries = build_multi_queries(
        question,
        dimensions,
        max_queries=RAG_MULTI_QUERY_MAX_QUERIES,
    )
    if not queries and question.strip():
        queries = [question.strip()]

    embeddings = get_embeddings_model()
    result_sets: list[list[Document]] = []
    for query_text in queries:
        try:
            docs = _vector_search_docs(
                query_text,
                user_id,
                embeddings,
                RAG_VECTOR_FETCH_K,
            )
            if docs:
                result_sets.append(docs)
        except Exception as exc:
            logger.warning(f"Multi-query recall failed for query='{query_text}': {exc}")

    fused_docs = _fuse_docs_with_rrf(result_sets, rrf_k=RAG_RRF_K, top_k=RAG_FUSION_TOP_K)
    retrieval_query = build_retrieval_query(question, dimensions)
    reranked_docs = rerank_documents(retrieval_query, fused_docs)
    logger.info(
        f"多查询融合检索完成: queries={len(queries)}, raw_sets={len(result_sets)}, fused={len(fused_docs)}, reranked={len(reranked_docs)}"
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


def retrieve_docs_with_rewrite(payload: dict):
    question = str(payload.get("question", "") or "")
    rewrite_output = payload.get("rewrite_output")
    current_user_id = int(payload["user_id"])

    dimensions = require_keyword_dimensions(rewrite_output)
    return multi_query_db_retriever(question, current_user_id, dimensions)


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
1. 每个字段都应该是英文短语数组。
2. 无内容时返回空数组。
3. 不要编造无关词。

问题：{question}
""")
    rewrite_llm = llm.with_structured_output(RewriteKeywordDimensions)
    rewriter = (rewrite_prompt | rewrite_llm).with_config({"run_name": "keyword_gen"})

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
