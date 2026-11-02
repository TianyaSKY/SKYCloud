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
    """单个账户的累计 Token 统计总量."""

    user_id: int
    total_prompt_tokens: int
    total_completion_tokens: int
    total_tokens: int
    last_active_at: str | None


class UserTokenStatsPayload(TokenStatsPayload):
    """账户身份标识及其累计 Token 统计总量."""

    username: str
    role: str | None
    avatar: str | None
    created_at: str | None


class DailyStatsPayload(TypedDict):
    """单日聚合用量数据统计."""

    date: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    request_count: int


class PerUserDailyStatsPayload(DailyStatsPayload):
    """单个账户在单日内的聚合用量统计."""

    user_id: int
    username: str


class UsagePagePayload(TypedDict):
    """用量审计日志单页数据."""

    total: int
    page: int
    page_size: int
    items: list[TokenUsageLogDict]


class AdminUsageEntryPayload(TokenUsageLogDict):
    """附带所属账户用户名的用量审计记录."""

    username: str


class AdminUsagePagePayload(TypedDict):
    """全站用量审计日志单页数据."""

    total: int
    page: int
    page_size: int
    items: list[AdminUsageEntryPayload]


@dataclass(frozen=True, slots=True, kw_only=True)
class RecordUsageParams:
    """单次 LLM 调用的 Token 记账记录模型."""

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
    """单个账户用量审计日志分页查询过滤参数."""

    user_id: int
    page: int = 1
    page_size: int = 20
    action: str | None = None
    start_date: str | None = None
    end_date: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class AdminUsageLogsParams:
    """全站用量审计日志分页查询过滤参数."""

    page: int = 1
    page_size: int = 20
    action: str | None = None
    user_id: int | None = None
    start_date: str | None = None
    end_date: str | None = None


def _usage_day(session: Session) -> ColumnElement[dt.date]:
    """返回本地 (Asia/Shanghai) 日历日表达式."""
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
        # 记账过程跨越会话工厂、SQLAlchemy 与驱动回调。
        # 任何失败均保持尽力而为, 仍会尝试回滚与关闭会话。
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
    """保持记账清理尽最大努力执行, 并在连接损坏时将其废弃."""
    try:
        getattr(session, operation)()
    except Exception as exc:
        # 驱动/连接池清理异常无法穷举。保留原始业务
        # 处理结果, 并将清理失败的连接句柄置为失效。
        logger.error(
            "Token usage session {} failed (error_type={})",
            operation,
            type(exc).__name__,
        )
        try:
            session.invalidate()
        except Exception as invalidation_exc:
            # 失效处理是最后的清理手段; 绝不能替换记账
            # 结果或对外暴露数据库连接细节。
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
    """拒绝非法的日期过滤条件, 而非静默放宽所请求的审计范围."""
    try:
        start = (
            to_utc_datetime(dt.datetime.fromisoformat(start_date))
            if start_date
            else None
        )
        if end_date:
            parsed_end = dt.datetime.fromisoformat(end_date)
            if parsed_end.date().isoformat() == end_date:
                # 仅包含日期的过滤条件应涵盖所选的整个日历日。
                # 使用该日期自身的最大时间点: 若采用递增日期的方式,
                # 则在极端上限年份时会溢出, 而输入本身是完全合法的。
                parsed_end = dt.datetime.combine(
                    parsed_end.date(),
                    dt.time.max,
                )
            end = to_utc_datetime(parsed_end)
        else:
            end = None
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


# 管理员接口


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
