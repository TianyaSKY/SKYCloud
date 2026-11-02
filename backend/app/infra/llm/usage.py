"""同步与异步客户端共享的尽力而为 Token 用量记录器."""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

from app.features.token_usage import service as token_usage_service


@dataclass(frozen=True, slots=True, kw_only=True)
class TokenUsage:
    """同步与异步 LLM 客户端通用的单条 Token 用量记录."""

    user_id: int
    action: str
    model_name: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    query_summary: str | None = None


def safe_record(usage: TokenUsage) -> None:
    """写入 Token 用量记录, 确保计量失败不会导致业务请求失败."""
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
        # 记账过程跨越 SQLAlchemy、会话与驱动层, 无法预先穷举异常;
        # 无论记账是否失败, 业务请求结果必须保持正常返回。
        logger.warning(
            "Failed to record token usage (error_type={})",
            type(exc).__name__,
        )
