"""Unified async LLM and embedding entry points with token accounting.

LangChain paths that cannot be intercepted still record usage through
``record_llm_usage``.
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

# ---------------------------------------------------------------------------
# Connection pool
# ---------------------------------------------------------------------------
_client_cache: dict[tuple[str, str, int], AsyncOpenAI] = {}


def _get_client(api_base: str, api_key: str) -> AsyncOpenAI:
    """Reuse an async OpenAI client per endpoint, key and event loop."""
    # The AsyncClient connection pool is bound to an event loop. API processes
    # normally own one loop; the worker uses the independent sync client.
    try:
        loop_id = id(asyncio.get_running_loop())
    except RuntimeError:
        # Allow command-line/test pre-creation; network calls still run in a
        # coroutine.
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
    """Close the process-wide async HTTP connection pools."""
    clients = list(_client_cache.values())
    _client_cache.clear()
    for client in clients:
        await client.close()


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


async def chat_completion(
    *,
    messages: Iterable[ChatCompletionMessageParam],
    config: dict[str, str],
    user_id: int = 0,
    action: str = "chat",
    query_summary: str | None = None,
) -> ChatCompletion:
    """Run an async chat completion and record its token usage."""
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
    """Embed texts and truncate vectors to the 1024 dimensions storage expects."""
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
    """Record usage from streaming LangChain paths that cannot be intercepted."""
    if usage.total_tokens <= 0 and usage.prompt_tokens <= 0:
        return
    _safe_record(usage)


# ---------------------------------------------------------------------------
# LangChain compatibility layer
# ---------------------------------------------------------------------------


class TrackingOpenAIEmbeddings(OpenAIEmbeddings):
    """LangChain embeddings wrapper that routes through ``embed_texts``.

    Request identity is passed per call; the cached object keeps no identity.
    """

    # The base accepts arbitrary kwargs; retain that contract. At the SDK
    # boundary int() still performs the original runtime conversion/validation
    # of user_id, so the cast neither rejects nor normalizes extra kwargs.

    def _as_config(self) -> dict[str, str]:
        """Project the LangChain client settings onto the shared config shape."""
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
        """Embed document batches through the shared accounting entry point."""
        if not texts:
            return []
        del chunk_size  # embed_texts sends one batch; base batching is unused.
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
        """Embed one query through the shared accounting entry point."""
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
