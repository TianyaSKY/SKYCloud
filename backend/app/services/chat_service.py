import json
import logging
from operator import itemgetter

from langchain_core.documents import Document
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableParallel, RunnableLambda
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from sqlalchemy import text

from app.extensions import db
from app.services.sys_dict_service import get_sys_dict_by_key

logger = logging.getLogger(__name__)

def get_chat_model():
    api_url = get_sys_dict_by_key('chat_api_url').value
    api_key = get_sys_dict_by_key('chat_api_key').value
    model_name = get_sys_dict_by_key('chat_api_model').value
    
    return ChatOpenAI(
        api_key=api_key,
        base_url=api_url,
        model=model_name,
        temperature=0,
    )

def get_embeddings_model():
    api_url = get_sys_dict_by_key('emb_api_url').value
    api_key = get_sys_dict_by_key('emb_api_key').value
    model_name = get_sys_dict_by_key('emb_model_name').value
    
    return OpenAIEmbeddings(
        api_key=api_key,
        base_url=api_url,
        model=model_name
    )

def custom_db_retriever(query_text: str, user_id: int):
    """自定义检索器，使用 SQLAlchemy 执行向量搜索"""
    embeddings = get_embeddings_model()
    query_vector = embeddings.embed_query(query_text)[:1536]
    
    sql = text("""
        SELECT id, name, description, mime_type
        FROM files
        WHERE description IS NOT NULL
          AND description != ''
          AND uploader_id = :user_id
        ORDER BY vector_info <=> :vector
        LIMIT 20
    """)
    
    results = db.session.execute(sql, {"vector": str(query_vector), "user_id": user_id}).fetchall()
    docs = []
    for row in results:
        content = f"文件名: {row[1]}\n描述: {row[2]}"
        metadata = {"id": row[0], "name": row[1], "mime_type": row[3]}
        docs.append(Document(page_content=content, metadata=metadata))
        
    logger.info(f"数据库检索到 {len(docs)} 条结果")
    return docs

def format_docs(docs):
    formatted = []
    for doc in docs:
        m = doc.metadata
        info = f"[文件: {m['name']} (ID: {m['id']})]\n{doc.page_content}"
        # 如果是图片，提示 AI 可以使用特定语法引用
        if m.get('mime_type', '').startswith('image/'):
            info += f"\n(这是一张图片，你可以使用 Markdown 语法展示它: ![图片](/api/file/download/{m['id']}))"
        formatted.append(info)
    return "\n\n---\n\n".join(formatted)

def format_history(history):
    if isinstance(history, list):
        return "\n".join([f"{h.get('role', 'user')}: {h.get('content', '')}" for h in history])
    return str(history) if history else ""

async def generate_chat_events(user_id, query: str, history: list):
    """异步生成器，用于 SSE 流式输出"""
    llm = get_chat_model()
    formatted_history = format_history(history)

    # 关键词重写链：使用中文提示词，但要求输出英文关键词（根据之前的要求）
    rewrite_prompt = ChatPromptTemplate.from_template("请从以下问题中提取核心搜索关键词：{question}。仅输出英文关键词。")
    rewriter = (rewrite_prompt | llm | StrOutputParser()).with_config({"run_name": "keyword_gen"})

    # 最终回答的提示词模板：使用中文提示词
    answer_prompt = ChatPromptTemplate.from_template("""
你是一个云盘助手。请根据以下参考信息回答用户问题。

1. 请使用中文回答。
2. 如果参考信息中有图片文件，且与问题相关，请在回答中使用 Markdown 语法 `![图片名](/api/file/download/文件ID)` 展示图片。
3. 如果信息不足，请根据已知内容回答。
4. 保持回答简洁专业。

历史对话：
{history}

参考信息：
{context}

问题：{question}
""")

    # 最终回答链
    answer_chain = (answer_prompt | llm | StrOutputParser()).with_config({"run_name": "final_answer"})

    rag_chain = (
            RunnableParallel({
                "context": {
                    "query_text": rewriter,
                    "user_id": itemgetter("user_id")
                } | RunnableLambda(lambda x: custom_db_retriever(x["query_text"], x["user_id"])) | format_docs,
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
            
            # 处理关键词生成
            if kind == "on_chain_end" and event["name"] == "keyword_gen":
                keywords = event["data"]["output"]
                yield f"data: {json.dumps({'type': 'keywords', 'content': keywords})}\n\n"
            
            # 处理最终回答的流
            elif kind == "on_chat_model_stream":
                content = event["data"]["chunk"].content
                if content:
                    yield f"data: {json.dumps({'type': 'token', 'content': content})}\n\n"
            
            # 处理状态
            elif kind == "on_retriever_start" or (kind == "on_chain_start" and event["name"] == "custom_db_retriever"):
                yield f"data: {json.dumps({'type': 'status', 'content': '🔍 正在检索相关文件...'})}\n\n"

    except Exception as e:
        logger.error(f"Chat error: {e}", exc_info=True)
        yield f"data: {json.dumps({'type': 'status', 'content': f'❌ 错误: {str(e)}'})}\n\n"
