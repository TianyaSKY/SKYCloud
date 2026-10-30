"""Synchronous OpenAI-compatible client used by the worker process.

The API process uses :mod:`app.infra.llm.client` and its async client cache.
This module deliberately has an independent cache because ``OpenAI`` clients
are synchronous and do not have an event-loop lifetime to manage.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from loguru import logger
from openai import OpenAI

from app.infra.llm.usage import TokenUsage
from app.infra.llm.usage import safe_record as _safe_record

if TYPE_CHECKING:
    from collections.abc import Iterable

    from openai.types import CreateEmbeddingResponse
    from openai.types.chat import ChatCompletion, ChatCompletionMessageParam

_client_cache: dict[tuple[str, str], OpenAI] = {}
_client_lock = threading.Lock()


def _get_client(api_base: str, api_key: str) -> OpenAI:
    """Return a thread-safe, process-local sync client for one endpoint/key."""
    cache_key = (api_base, api_key)
    with _client_lock:
        client = _client_cache.get(cache_key)
        if client is None:
            client = OpenAI(api_key=api_key, base_url=api_base, timeout=120)
            _client_cache[cache_key] = client
        return client


def close_sync_clients() -> None:
    """Close all cached sync HTTP clients during worker shutdown."""
    with _client_lock:
        clients = list(_client_cache.values())
        _client_cache.clear()

    for client in clients:
        try:
            client.close()
        except Exception as exc:
            # Shutdown cleanup must attempt every client; the SDK transport can
            # raise an unbounded set, so one failure cannot abort the loop.
            logger.warning(
                "Failed to close sync LLM client (error_type={})",
                type(exc).__name__,
            )


def chat_completion(
    *,
    messages: Iterable[ChatCompletionMessageParam],
    config: dict[str, str],
    user_id: int = 0,
    action: str = "chat",
    query_summary: str | None = None,
) -> ChatCompletion:
    """Create a synchronous chat completion and record returned usage."""
    client = _get_client(config["api"], config["key"])
    model = config.get("model", "")
    response = client.chat.completions.create(model=model, messages=messages)

    if response.usage:
        _safe_record(
            TokenUsage(
                user_id=user_id,
                action=action,
                model_name=model,
                prompt_tokens=response.usage.prompt_tokens or 0,
                completion_tokens=response.usage.completion_tokens or 0,
                total_tokens=response.usage.total_tokens or 0,
                query_summary=query_summary,
            ),
        )
    return response


def embed_texts(
    *,
    texts: list[str] | str,
    config: dict[str, str],
    user_id: int = 0,
    query_summary: str | None = None,
) -> list[list[float]]:
    """Create synchronous embeddings, preserving input order and max dimension."""
    if isinstance(texts, str):
        texts = [texts]
    if not texts:
        return []

    client = _get_client(config["api"], config["key"])
    model = config.get("model", "Qwen/Qwen3-Embedding-8B")
    response: CreateEmbeddingResponse = client.embeddings.create(
        model=model,
        input=texts,
    )
    sorted_data = sorted(response.data, key=lambda item: item.index)
    vectors = [item.embedding[:1024] for item in sorted_data]

    usage = response.usage
    total_tokens = usage.total_tokens if usage else 0
    if usage and total_tokens:
        _safe_record(
            TokenUsage(
                user_id=user_id,
                action="embedding",
                model_name=model,
                prompt_tokens=usage.prompt_tokens or total_tokens,
                completion_tokens=0,
                total_tokens=total_tokens,
                query_summary=query_summary,
            ),
        )
    return vectors


def record_llm_usage(*, usage: TokenUsage) -> None:
    """Record usage from synchronous LangChain worker flows."""
    if usage.total_tokens <= 0 and usage.prompt_tokens <= 0:
        return
    _safe_record(usage)
