"""
统一 LLM 调用接口

所有 LLM / Embedding API 调用统一通过本模块发起，自动记录 token 用量。
对于 LangChain 流式场景（无法替换底层调用），提供 record_llm_usage() 手动记录。

使用示例:
    from app.services.llm_client import chat_completion, embed_texts

    # Chat
    resp = chat_completion(
        messages=[{"role": "user", "content": "hello"}],
        config=get_chat_model_config(),
        user_id=1,
        action="describe_text",
    )
    answer = resp.choices[0].message.content

    # Embedding
    vectors = embed_texts(
        texts=["hello world"],
        config=get_embedding_model_config(),
        user_id=1,
    )
"""

import logging
from typing import Any

from openai import OpenAI
from langchain_openai import OpenAIEmbeddings

logger = logging.getLogger(__name__)

# ---- 连接池 ----
_client_cache: dict[tuple, OpenAI] = {}


def _get_client(api_base: str, api_key: str) -> OpenAI:
    """获取或复用 OpenAI 客户端实例"""
    cache_key = (api_base, api_key)
    if cache_key not in _client_cache:
        _client_cache[cache_key] = OpenAI(
            api_key=api_key, base_url=api_base, timeout=120
        )
    return _client_cache[cache_key]


# ---- 安全记录 ----
def _safe_record(
    user_id: int,
    action: str,
    model_name: str | None,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    query_summary: str | None = None,
) -> None:
    """安全写入 token 用量，异常不外抛"""
    try:
        from app.services.token_usage_service import record_usage

        record_usage(
            user_id=user_id,
            action=action,
            model_name=model_name,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            query_summary=query_summary,
        )
    except Exception as e:
        logger.warning(f"Failed to record token usage ({action}): {e}")


# ===================== 公开接口 =====================


def chat_completion(
    *,
    messages: list[dict[str, Any]],
    config: dict[str, str],
    user_id: int = 0,
    action: str = "chat",
    query_summary: str | None = None,
    **kwargs: Any,
) -> Any:
    """同步 Chat Completion 调用，自动记录 token。

    Args:
        messages: OpenAI 格式的消息列表
        config: 模型配置，需包含 api / key / model
        user_id: 关联的用户 ID
        action: 记录到日志的 action 标签
        query_summary: 可选摘要
        **kwargs: 透传给 OpenAI client（如 temperature 等）

    Returns:
        OpenAI ChatCompletion 响应对象
    """
    client = _get_client(config["api"], config["key"])
    model = config.get("model", "")

    resp = client.chat.completions.create(
        model=model,
        messages=messages,
        **kwargs,
    )

    if resp.usage:
        _safe_record(
            user_id=user_id,
            action=action,
            model_name=model,
            prompt_tokens=resp.usage.prompt_tokens or 0,
            completion_tokens=resp.usage.completion_tokens or 0,
            total_tokens=resp.usage.total_tokens or 0,
            query_summary=query_summary,
        )

    return resp


def embed_texts(
    *,
    texts: list[str] | str,
    config: dict[str, str],
    user_id: int = 0,
    query_summary: str | None = None,
) -> list[list[float]]:
    """Embedding 调用，返回向量列表（截取前 1024 维），自动记录 token。

    Args:
        texts: 单条文本或文本列表
        config: 模型配置，需包含 api / key / model
        user_id: 关联的用户 ID
        query_summary: 可选摘要

    Returns:
        向量列表，与输入 texts 顺序一致
    """
    if isinstance(texts, str):
        texts = [texts]
    if not texts:
        return []

    client = _get_client(config["api"], config["key"])
    model = config.get("model", "Qwen/Qwen3-Embedding-8B")

    resp = client.embeddings.create(model=model, input=texts)
    sorted_data = sorted(resp.data, key=lambda x: x.index)
    vectors = [item.embedding[:1024] for item in sorted_data]

    if resp.usage and resp.usage.total_tokens:
        _safe_record(
            user_id=user_id,
            action="embedding",
            model_name=model,
            prompt_tokens=resp.usage.prompt_tokens or resp.usage.total_tokens,
            completion_tokens=0,
            total_tokens=resp.usage.total_tokens,
            query_summary=query_summary,
        )

    return vectors


def record_llm_usage(
    *,
    user_id: int,
    action: str,
    model_name: str | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    query_summary: str | None = None,
) -> None:
    """手动记录 token 用量（适用于 LangChain 等外部框架自行管理 API 调用的场景）。

    LangChain 的 ChatOpenAI / create_react_agent 内部发起 API 请求，
    token 信息通过 usage_metadata / on_chat_model_end 事件获取后，
    调用此方法统一写入。
    """
    if total_tokens <= 0 and prompt_tokens <= 0:
        return
    _safe_record(
        user_id=user_id,
        action=action,
        model_name=model_name,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        query_summary=query_summary,
    )


# ===================== LangChain 兼容层 =====================


class TrackingOpenAIEmbeddings(OpenAIEmbeddings):
    """LangChain OpenAIEmbeddings 的追踪包装器。

    覆盖 embed_documents / embed_query，通过统一的 embed_texts() 发起调用，
    确保 token 用量被自动记录。

    用法:
        emb = TrackingOpenAIEmbeddings(api_key=..., base_url=..., model=...)
        emb.set_tracking_user(user_id)
    """

    _tracking_user_id: int = 0

    def set_tracking_user(self, user_id: int) -> None:
        self._tracking_user_id = user_id

    def _as_config(self) -> dict[str, str]:
        return {
            "api": str(self.openai_api_base or ""),
            "key": str(self.openai_api_key or ""),
            "model": self.model,
        }

    def embed_documents(
        self, texts: list[str], chunk_size: int | None = None
    ) -> list[list[float]]:
        if not texts:
            return []
        return embed_texts(
            texts=texts,
            config=self._as_config(),
            user_id=self._tracking_user_id,
            query_summary=f"chat_rag({len(texts)} texts)",
        )

    def embed_query(self, text: str) -> list[float]:
        result = embed_texts(
            texts=[text],
            config=self._as_config(),
            user_id=self._tracking_user_id,
            query_summary=f"chat_rag_query",
        )
        return result[0] if result else []
