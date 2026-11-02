"""统一的异步 LLM 与嵌入向量调用入口, 集成 Token 计量与记录.

无法被中间拦截的 LangChain 路径仍可通过 ``record_llm_usage`` 进行用量记录。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

from langchain_openai import OpenAIEmbeddings
from openai import AsyncOpenAI
from pydantic import SecretStr

from app.infra.llm.usage import TokenUsage
from app.infra.llm.usage import safe_record as _safe_record
from app.infra.owned_work import run_owned_persistent

if TYPE_CHECKING:
    from collections.abc import Iterable
    from typing import SupportsIndex, SupportsInt

    from openai.types.chat import ChatCompletion, ChatCompletionMessageParam

# 连接池
_client_cache: dict[tuple[str, str, int], AsyncOpenAI] = {}


def _get_client(api_base: str, api_key: str) -> AsyncOpenAI:
    """按接口端点、API 密钥与事件循环复用异步 OpenAI 客户端."""
    # AsyncClient 连接池绑定到特定事件循环。API 进程
    # 通常拥有一个主循环; worker 进程则使用独立的同步客户端。
    try:
        loop_id = id(asyncio.get_running_loop())
    except RuntimeError:
        # 允许命令行/测试预先创建; 网络调用仍运行在协程中。
        loop_id = 0
    cache_key = (api_base, api_key, loop_id)
    if cache_key not in _client_cache:
        _client_cache[cache_key] = AsyncOpenAI(
            api_key=api_key,
            base_url=api_base,
            timeout=120,
        )
    return _client_cache[cache_key]


async def close_llm_clients() -> None:
    """关闭进程级的异步 HTTP 连接池."""
    clients = list(_client_cache.values())
    _client_cache.clear()
    for client in clients:
        await client.close()


# 公共接口


async def chat_completion(
    *,
    messages: Iterable[ChatCompletionMessageParam],
    config: dict[str, str],
    user_id: int = 0,
    action: str = "chat",
    query_summary: str | None = None,
) -> ChatCompletion:
    """执行异步聊天补全并记录其 Token 用量."""
    client = _get_client(config["api"], config["key"])
    model = config.get("model", "")

    resp = await client.chat.completions.create(model=model, messages=messages)

    if resp.usage:
        await run_owned_persistent(
            _safe_record,
            TokenUsage(
                user_id=user_id,
                action=action,
                model_name=model,
                prompt_tokens=resp.usage.prompt_tokens or 0,
                completion_tokens=resp.usage.completion_tokens or 0,
                total_tokens=resp.usage.total_tokens or 0,
                query_summary=query_summary,
            ),
        )

    return resp


async def embed_texts(
    *,
    texts: list[str] | str,
    config: dict[str, str],
    user_id: int = 0,
    query_summary: str | None = None,
) -> list[list[float]]:
    """对文本生成嵌入向量, 并将向量截断至存储所需的 1024 维度."""
    if isinstance(texts, str):
        texts = [texts]
    if not texts:
        return []

    client = _get_client(config["api"], config["key"])
    model = config.get("model", "Qwen/Qwen3-Embedding-8B")

    resp = await client.embeddings.create(model=model, input=texts)
    sorted_data = sorted(resp.data, key=lambda x: x.index)
    vectors = [item.embedding[:1024] for item in sorted_data]

    if resp.usage and resp.usage.total_tokens:
        await run_owned_persistent(
            _safe_record,
            TokenUsage(
                user_id=user_id,
                action="embedding",
                model_name=model,
                prompt_tokens=resp.usage.prompt_tokens or resp.usage.total_tokens,
                completion_tokens=0,
                total_tokens=resp.usage.total_tokens,
                query_summary=query_summary,
            ),
        )

    return vectors


def record_llm_usage(*, usage: TokenUsage) -> None:
    """记录无法直接拦截的流式 LangChain 路径的用量."""
    if usage.total_tokens <= 0 and usage.prompt_tokens <= 0:
        return
    _safe_record(usage)


# LangChain 兼容层


class TrackingOpenAIEmbeddings(OpenAIEmbeddings):
    """经由 ``embed_texts`` 路由调用的 LangChain embeddings 包装器.

    请求身份按每次调用传入; 缓存的对象不保留任何请求身份。
    """

    # 基类接受任意 kwargs; 保留该契约。在 SDK
    # 边界处 int() 仍会执行 user_id 的原始运行时转换与校验。

    def _as_config(self) -> dict[str, str]:
        """将 LangChain 客户端设置映射到共享的配置结构."""
        api_key = self.openai_api_key
        if isinstance(api_key, SecretStr):
            key_str = api_key.get_secret_value()
        else:
            key_str = str(api_key or "")
        return {
            "api": str(self.openai_api_base or ""),
            "key": key_str,
            "model": self.model,
        }

    async def aembed_documents(
        self,
        texts: list[str],
        chunk_size: int | None = None,
        **kwargs: object,
    ) -> list[list[float]]:
        """通过共享的计量入口对批量文档生成嵌入向量."""
        if not texts:
            return []
        del chunk_size  # embed_texts 发送单个批次; 基类的批处理逻辑未使用。
        return await embed_texts(
            texts=texts,
            config=self._as_config(),
            user_id=int(
                cast(
                    "str | bytes | bytearray | SupportsInt | SupportsIndex",
                    kwargs.get("user_id") or 0,
                ),
            ),
            query_summary=f"chat_rag({len(texts)} texts)",
        )

    async def aembed_query(
        self,
        text: str,
        **kwargs: object,
    ) -> list[float]:
        """通过共享的计量入口对单条查询生成嵌入向量."""
        result = await embed_texts(
            texts=[text],
            config=self._as_config(),
            user_id=int(
                cast(
                    "str | bytes | bytearray | SupportsInt | SupportsIndex",
                    kwargs.get("user_id") or 0,
                ),
            ),
            query_summary="chat_rag_query",
        )
        return result[0] if result else []
