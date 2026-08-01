"""Synchronous OpenAI-compatible client used by the worker process.

The API process uses :mod:`app.infra.llm.client` and its async client cache.
This module deliberately has an independent cache because ``OpenAI`` clients
are synchronous and do not have an event-loop lifetime to manage.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from openai import OpenAI

from app.infra.llm.usage import safe_record as _safe_record

logger = logging.getLogger(__name__)

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
            logger.warning("Failed to close sync LLM client: %s", exc)


def chat_completion(
    *,
    messages: list[dict[str, Any]],
    config: dict[str, str],
    user_id: int = 0,
    action: str = "chat",
    query_summary: str | None = None,
    **kwargs: Any,
) -> Any:
    """Create a synchronous chat completion and record returned usage."""
    client = _get_client(config["api"], config["key"])
    model = config.get("model", "")
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        **kwargs,
    )

    usage = getattr(response, "usage", None)
    if usage:
        _safe_record(
            user_id=user_id,
            action=action,
            model_name=model,
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            total_tokens=getattr(usage, "total_tokens", 0) or 0,
            query_summary=query_summary,
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
    response = client.embeddings.create(model=model, input=texts)
    sorted_data = sorted(response.data, key=lambda item: item.index)
    vectors = [item.embedding[:1024] for item in sorted_data]

    usage = getattr(response, "usage", None)
    total_tokens = getattr(usage, "total_tokens", 0) if usage else 0
    if total_tokens:
        _safe_record(
            user_id=user_id,
            action="embedding",
            model_name=model,
            prompt_tokens=(getattr(usage, "prompt_tokens", 0) or total_tokens),
            completion_tokens=0,
            total_tokens=total_tokens,
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
    """Record usage from synchronous LangChain worker flows."""
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
