"""统一 LLM / Embedding 调用入口，自动记录 token 用量。

LangChain 等无法替换底层调用的场景，用 record_llm_usage() 手动补记。
"""

import logging
from typing import Any

from openai import OpenAI
from langchain_openai import OpenAIEmbeddings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 连接池
# ---------------------------------------------------------------------------
_client_cache: dict[tuple, OpenAI] = {}


def _get_client(api_base: str, api_key: str) -> OpenAI:
    """按 (api_base, api_key) 复用 OpenAI 客户端，减少连接开销。"""
    cache_key = (api_base, api_key)
    if cache_key not in _client_cache:
        _client_cache[cache_key] = OpenAI(
            api_key=api_key, base_url=api_base, timeout=120
        )
    return _client_cache[cache_key]


# ---------------------------------------------------------------------------
# 安全记录
# ---------------------------------------------------------------------------
def _safe_record(
    user_id: int,
    action: str,
    model_name: str | None,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    query_summary: str | None = None,
) -> None:
    """写 token 用量；异常吞掉，避免用量记录拖垮主链路。"""
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


# ---------------------------------------------------------------------------
# 公开接口
# ---------------------------------------------------------------------------


def chat_completion(
    *,
    messages: list[dict[str, Any]],
    config: dict[str, str],
    user_id: int = 0,
    action: str = "chat",
    query_summary: str | None = None,
    **kwargs: Any,
) -> Any:
    """同步 Chat Completion；成功后按 resp.usage 自动记 token。

    config 需含 api / key / model；kwargs 透传给 OpenAI client。
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
    """Embedding 调用；向量截取前 1024 维以匹配 DB 列维度。"""
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
    """手动记 token（LangChain 流式等无法拦截底层 API 的场景）。

    用量为 0 时跳过，避免空记录噪音。
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


# ---------------------------------------------------------------------------
# LangChain 兼容层
# ---------------------------------------------------------------------------


class TrackingOpenAIEmbeddings(OpenAIEmbeddings):
    """LangChain Embeddings 包装：走 embed_texts 以统一记 token。

    调用前 set_tracking_user(user_id) 才能正确归属用量。
    """

    _tracking_user_id: int = 0

    def set_tracking_user(self, user_id: int) -> None:
        self._tracking_user_id = user_id

    def _as_config(self) -> dict[str, str]:
        api_key = self.openai_api_key
        if hasattr(api_key, "get_secret_value"):
            key_str = api_key.get_secret_value()
        else:
            key_str = str(api_key or "")
        return {
            "api": str(self.openai_api_base or ""),
            "key": key_str,
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
