"""Shared, best-effort token usage recording for sync and async clients."""

from __future__ import annotations

from loguru import logger


def safe_record(
    user_id: int,
    action: str,
    model_name: str | None,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    query_summary: str | None = None,
) -> None:
    """Write usage without allowing accounting failures to break the request."""
    try:
        from app.features.token_usage.service import record_usage

        record_usage(
            user_id=user_id,
            action=action,
            model_name=model_name,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            query_summary=query_summary,
        )
    except Exception as exc:
        logger.warning("Failed to record token usage ({}): {}", action, exc)
