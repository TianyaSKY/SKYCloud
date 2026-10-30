"""Token 用量记录与查询:明细落库,并原子累加 users 表统计."""

import datetime as dt
from dataclasses import dataclass
from typing import TypedDict

from loguru import logger
from sqlalchemy import ColumnElement, func, text
from sqlalchemy.orm import Session

from app.exceptions import BusinessRuleError, ResourceNotFoundError
from app.infra.datetime_utils import local_isoformat, to_utc_datetime, utc_now
from app.infra.extensions import SessionLocal
from app.models.token_usage_log import TokenUsageLog, TokenUsageLogDict
from app.models.user import User

_NOT_FOUND = "User not found"


class TokenStatsPayload(TypedDict):
    """Accumulated token totals for one account."""

    user_id: int
    total_prompt_tokens: int
    total_completion_tokens: int
    total_tokens: int
    last_active_at: str | None


class UserTokenStatsPayload(TokenStatsPayload):
    """Account identity plus its accumulated token totals."""

    username: str
    role: str | None
    avatar: str | None
    created_at: str | None


class DailyStatsPayload(TypedDict):
    """One day of aggregated usage."""

    date: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    request_count: int


class PerUserDailyStatsPayload(DailyStatsPayload):
    """One day of aggregated usage for a single account."""

    user_id: int
    username: str


class UsagePagePayload(TypedDict):
    """One page of usage audit rows."""

    total: int
    page: int
    page_size: int
    items: list[TokenUsageLogDict]


class AdminUsageEntryPayload(TokenUsageLogDict):
    """Usage audit row enriched with the owning account name."""

    username: str


class AdminUsagePagePayload(TypedDict):
    """One page of site-wide usage audit rows."""

    total: int
    page: int
    page_size: int
    items: list[AdminUsageEntryPayload]


@dataclass(frozen=True, slots=True, kw_only=True)
class RecordUsageParams:
    """One LLM call's token accounting record."""

    user_id: int
    action: str
    model_name: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    query_summary: str | None = None
    extra_info: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class GetUsageLogsParams:
    """Filters for one account's usage audit page."""

    user_id: int
    page: int = 1
    page_size: int = 20
    action: str | None = None
    start_date: str | None = None
    end_date: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class AdminUsageLogsParams:
    """Filters for the site-wide usage audit page."""

    page: int = 1
    page_size: int = 20
    action: str | None = None
    user_id: int | None = None
    start_date: str | None = None
    end_date: str | None = None


def _usage_day(session: Session) -> ColumnElement[dt.date]:
    """Return the local (Asia/Shanghai) calendar day expression."""
    if session.get_bind().dialect.name == "postgresql":
        return func.date(func.timezone("Asia/Shanghai", TokenUsageLog.created_at))
    return func.date(TokenUsageLog.created_at, "+8 hours")


def record_usage(params: RecordUsageParams) -> None:
    """记录一次用量并累加用户统计.

    使用独立 session,避免干扰调用方事务;失败只打日志.
    """
    total_tokens = params.total_tokens
    session: Session | None = None
    try:
        if total_tokens == 0:
            total_tokens = params.prompt_tokens + params.completion_tokens
        session = SessionLocal()
        log = TokenUsageLog(
            user_id=params.user_id,
            action=params.action,
            model_name=params.model_name,
            prompt_tokens=params.prompt_tokens,
            completion_tokens=params.completion_tokens,
            total_tokens=total_tokens,
            query_summary=(
                params.query_summary[:200] if params.query_summary else None
            ),
            extra_info=params.extra_info,
        )
        session.add(log)

        # 原子累加,避免并发读改写丢失
        session.execute(
            text(
                """
                UPDATE users SET
                    total_prompt_tokens = COALESCE(total_prompt_tokens, 0) + :pt,
                    total_completion_tokens =
                        COALESCE(total_completion_tokens, 0) + :ct,
                    total_tokens = COALESCE(total_tokens, 0) + :tt,
                    last_active_at = :now
                WHERE id = :uid
                """,
            ),
            {
                "pt": params.prompt_tokens,
                "ct": params.completion_tokens,
                "tt": total_tokens,
                "uid": params.user_id,
                "now": utc_now(),
            },
        )
        session.commit()
    except Exception as exc:
        # Accounting crosses session factories, SQLAlchemy and driver callbacks.
        # Any failure stays best-effort, with rollback/close still attempted.
        logger.error(
            "Failed to record token usage for user {} (error_type={})",
            params.user_id,
            type(exc).__name__,
        )
        if session is not None:
            _cleanup_usage_session(session, "rollback")
    finally:
        if session is not None:
            _cleanup_usage_session(session, "close")


def _cleanup_usage_session(session: Session, operation: str) -> None:
    """Keep accounting cleanup best-effort and discard a damaged connection."""
    try:
        getattr(session, operation)()
    except Exception as exc:
        # Driver/pool cleanup exceptions are not a closed set. Keep the original
        # business result and invalidate a connection whose cleanup failed.
        logger.error(
            "Token usage session {} failed (error_type={})",
            operation,
            type(exc).__name__,
        )
        try:
            session.invalidate()
        except Exception as invalidation_exc:
            # Invalidation is the last cleanup attempt; it must not replace the
            # accounting outcome or expose connection details.
            logger.error(
                "Token usage session invalidation failed (error_type={})",
                type(invalidation_exc).__name__,
            )


def get_user_token_stats(session: Session, user_id: int) -> TokenStatsPayload:
    """用户累计 token 统计(读 users 表冗余字段)."""
    user = session.get(User, user_id)
    if not user:
        raise ResourceNotFoundError(_NOT_FOUND)
    return {
        "user_id": user_id,
        "total_prompt_tokens": user.total_prompt_tokens or 0,
        "total_completion_tokens": user.total_completion_tokens or 0,
        "total_tokens": user.total_tokens or 0,
        "last_active_at": local_isoformat(user.last_active_at),
    }


def _date_bounds(
    start_date: str | None,
    end_date: str | None,
) -> tuple[dt.datetime | None, dt.datetime | None]:
    """Reject invalid filters rather than silently widening the requested audit."""
    try:
        start = (
            to_utc_datetime(dt.datetime.fromisoformat(start_date))
            if start_date
            else None
        )
        end = (
            to_utc_datetime(dt.datetime.fromisoformat(end_date))
            if end_date
            else None
        )
    except (ValueError, TypeError) as exc:
        message = "Invalid usage date filter"
        raise BusinessRuleError(message) from exc
    if start is not None and end is not None and start > end:
        message = "Usage start date must not follow end date"
        raise BusinessRuleError(message)
    return start, end


def get_usage_logs(session: Session, params: GetUsageLogsParams) -> UsagePagePayload:
    """分页查询用户用量明细,可按 action / 日期过滤."""
    query = session.query(TokenUsageLog).filter(
        TokenUsageLog.user_id == params.user_id,
    )

    if params.action:
        query = query.filter(TokenUsageLog.action == params.action)

    start, end = _date_bounds(params.start_date, params.end_date)
    if start is not None:
        query = query.filter(TokenUsageLog.created_at >= start)
    if end is not None:
        query = query.filter(TokenUsageLog.created_at <= end)

    total = query.count()
    logs = (
        query.order_by(TokenUsageLog.created_at.desc())
        .offset((params.page - 1) * params.page_size)
        .limit(params.page_size)
        .all()
    )

    return {
        "total": total,
        "page": params.page,
        "page_size": params.page_size,
        "items": [log.to_dict() for log in logs],
    }


def get_daily_stats(
    session: Session, user_id: int, days: int = 30,
) -> list[DailyStatsPayload]:
    """用户最近 N 天每日聚合统计."""
    since = utc_now() - dt.timedelta(days=days)
    rows = (
        session.query(
            _usage_day(session).label("date"),
            func.sum(TokenUsageLog.prompt_tokens).label("prompt_tokens"),
            func.sum(TokenUsageLog.completion_tokens).label("completion_tokens"),
            func.sum(TokenUsageLog.total_tokens).label("total_tokens"),
            func.count(TokenUsageLog.id).label("request_count"),
        )
        .filter(
            TokenUsageLog.user_id == user_id,
            TokenUsageLog.created_at >= since,
        )
        .group_by(_usage_day(session))
        .order_by(_usage_day(session))
        .all()
    )
    return [
        {
            "date": str(row.date),
            "prompt_tokens": int(row.prompt_tokens or 0),
            "completion_tokens": int(row.completion_tokens or 0),
            "total_tokens": int(row.total_tokens or 0),
            "request_count": int(row.request_count or 0),
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# 管理员接口
# ---------------------------------------------------------------------------


def get_all_users_token_stats(session: Session) -> list[UserTokenStatsPayload]:
    """全站用户累计 token 排行(admin)."""
    users = session.query(User).order_by(User.total_tokens.desc()).all()
    return [
        {
            "user_id": u.id,
            "username": u.username,
            "role": u.role,
            "avatar": u.avatar,
            "total_prompt_tokens": u.total_prompt_tokens or 0,
            "total_completion_tokens": u.total_completion_tokens or 0,
            "total_tokens": u.total_tokens or 0,
            "last_active_at": local_isoformat(u.last_active_at),
            "created_at": local_isoformat(u.created_at),
        }
        for u in users
    ]


def get_all_users_usage_logs(
    session: Session, params: AdminUsageLogsParams,
) -> AdminUsagePagePayload:
    """全站用量明细分页(admin),附带 username."""
    query = session.query(TokenUsageLog, User.username).outerjoin(
        User, TokenUsageLog.user_id == User.id,
    )

    if params.user_id:
        query = query.filter(TokenUsageLog.user_id == params.user_id)

    if params.action:
        query = query.filter(TokenUsageLog.action == params.action)

    start, end = _date_bounds(params.start_date, params.end_date)
    if start is not None:
        query = query.filter(TokenUsageLog.created_at >= start)
    if end is not None:
        query = query.filter(TokenUsageLog.created_at <= end)

    total = query.count()
    rows = (
        query.order_by(TokenUsageLog.created_at.desc())
        .offset((params.page - 1) * params.page_size)
        .limit(params.page_size)
        .all()
    )

    items: list[AdminUsageEntryPayload] = []
    for log, username in rows:
        items.append(
            {
                "id": log.id,
                "user_id": log.user_id,
                "action": log.action,
                "model_name": log.model_name,
                "prompt_tokens": log.prompt_tokens,
                "completion_tokens": log.completion_tokens,
                "total_tokens": log.total_tokens,
                "query_summary": log.query_summary,
                "extra_info": log.extra_info,
                "created_at": local_isoformat(log.created_at),
                "username": username or f"user_{log.user_id}",
            },
        )

    return {
        "total": total,
        "page": params.page,
        "page_size": params.page_size,
        "items": items,
    }


def get_all_users_daily_stats(
    session: Session, days: int = 30,
) -> list[DailyStatsPayload]:
    """全站合计最近 N 天每日统计(admin)."""
    since = utc_now() - dt.timedelta(days=days)
    rows = (
        session.query(
            _usage_day(session).label("date"),
            func.sum(TokenUsageLog.prompt_tokens).label("prompt_tokens"),
            func.sum(TokenUsageLog.completion_tokens).label("completion_tokens"),
            func.sum(TokenUsageLog.total_tokens).label("total_tokens"),
            func.count(TokenUsageLog.id).label("request_count"),
        )
        .filter(TokenUsageLog.created_at >= since)
        .group_by(_usage_day(session))
        .order_by(_usage_day(session))
        .all()
    )
    return [
        {
            "date": str(row.date),
            "prompt_tokens": int(row.prompt_tokens or 0),
            "completion_tokens": int(row.completion_tokens or 0),
            "total_tokens": int(row.total_tokens or 0),
            "request_count": int(row.request_count or 0),
        }
        for row in rows
    ]


def get_per_user_daily_stats(
    session: Session, days: int = 30,
) -> list[PerUserDailyStatsPayload]:
    """按用户拆分的最近 N 天每日统计(admin)."""
    since = utc_now() - dt.timedelta(days=days)
    rows = (
        session.query(
            TokenUsageLog.user_id,
            User.username,
            _usage_day(session).label("date"),
            func.sum(TokenUsageLog.prompt_tokens).label("prompt_tokens"),
            func.sum(TokenUsageLog.completion_tokens).label("completion_tokens"),
            func.sum(TokenUsageLog.total_tokens).label("total_tokens"),
            func.count(TokenUsageLog.id).label("request_count"),
        )
        .outerjoin(User, TokenUsageLog.user_id == User.id)
        .filter(TokenUsageLog.created_at >= since)
        .group_by(TokenUsageLog.user_id, User.username, _usage_day(session))
        .order_by(_usage_day(session))
        .all()
    )
    return [
        {
            "user_id": row.user_id,
            "username": row.username or f"user_{row.user_id}",
            "date": str(row.date),
            "prompt_tokens": int(row.prompt_tokens or 0),
            "completion_tokens": int(row.completion_tokens or 0),
            "total_tokens": int(row.total_tokens or 0),
            "request_count": int(row.request_count or 0),
        }
        for row in rows
    ]
