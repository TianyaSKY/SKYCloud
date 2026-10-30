"""Shared, best-effort token usage recording for sync and async clients."""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

from app.features.token_usage import service as token_usage_service


@dataclass(frozen=True, slots=True, kw_only=True)
class TokenUsage:
    """One token-usage record shared by the sync and async LLM clients."""

    user_id: int
    action: str
    model_name: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    query_summary: str | None = None


def safe_record(usage: TokenUsage) -> None:
    """Write usage without allowing accounting failures to break the request."""
    try:
        token_usage_service.record_usage(
            token_usage_service.RecordUsageParams(
                user_id=usage.user_id,
                action=usage.action,
                model_name=usage.model_name,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                total_tokens=usage.total_tokens,
                query_summary=usage.query_summary,
            ),
        )
    except Exception as exc:
        # Accounting crosses SQLAlchemy, session and driver layers, so no
        # bounded exception set exists; the request result must still survive.
        logger.warning(
            "Failed to record token usage (error_type={})",
            type(exc).__name__,
        )
