"""Token 用量记录与查询：明细落库，并原子累加 users 表统计。"""

import logging
from datetime import datetime, timedelta

from sqlalchemy import func, text

from app.infra.datetime_utils import beijing_now, local_isoformat
from app.extensions import db, SessionLocal
from app.models.token_usage_log import TokenUsageLog
from app.models.user import User

logger = logging.getLogger(__name__)


def record_usage(
    user_id: int,
    action: str,
    model_name: str | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    query_summary: str | None = None,
    extra_info: str | None = None,
) -> None:
    """记录一次用量并累加用户统计。

    使用独立 session，避免干扰调用方事务；失败只打日志。
    """
    if total_tokens == 0:
        total_tokens = prompt_tokens + completion_tokens

    session = SessionLocal()
    try:
        log = TokenUsageLog(
            user_id=user_id,
            action=action,
            model_name=model_name,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            query_summary=query_summary[:200] if query_summary else None,
            extra_info=extra_info,
        )
        session.add(log)

        # 原子累加，避免并发读改写丢失
        session.execute(
            text(
                """
                UPDATE users
                SET total_prompt_tokens    = COALESCE(total_prompt_tokens, 0) + :pt,
                    total_completion_tokens = COALESCE(total_completion_tokens, 0) + :ct,
                    total_tokens           = COALESCE(total_tokens, 0) + :tt,
                    last_active_at         = :now
                WHERE id = :uid
                """
            ),
            {
                "pt": prompt_tokens,
                "ct": completion_tokens,
                "tt": total_tokens,
                "uid": user_id,
                "now": beijing_now(),
            },
        )
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("Failed to record token usage for user %s", user_id)
    finally:
        session.close()


def get_user_token_stats(user_id: int) -> dict:
    """用户累计 token 统计（读 users 表冗余字段）。"""
    user = db.session.get(User, user_id)
    if not user:
        return {}
    return {
        "user_id": user_id,
        "total_prompt_tokens": user.total_prompt_tokens or 0,
        "total_completion_tokens": user.total_completion_tokens or 0,
        "total_tokens": user.total_tokens or 0,
        "last_active_at": local_isoformat(user.last_active_at),
    }


def get_usage_logs(
    user_id: int,
    page: int = 1,
    page_size: int = 20,
    action: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """分页查询用户用量明细，可按 action / 日期过滤。"""
    query = db.session.query(TokenUsageLog).filter(
        TokenUsageLog.user_id == user_id
    )

    if action:
        query = query.filter(TokenUsageLog.action == action)

    if start_date:
        try:
            sd = datetime.fromisoformat(start_date)
            query = query.filter(TokenUsageLog.created_at >= sd)
        except ValueError:
            pass

    if end_date:
        try:
            ed = datetime.fromisoformat(end_date)
            query = query.filter(TokenUsageLog.created_at <= ed)
        except ValueError:
            pass

    total = query.count()
    logs = (
        query.order_by(TokenUsageLog.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [log.to_dict() for log in logs],
    }


def get_daily_stats(user_id: int, days: int = 30) -> list[dict]:
    """用户最近 N 天每日聚合统计。"""
    since = beijing_now() - timedelta(days=days)
    rows = (
        db.session.query(
            func.date(TokenUsageLog.created_at).label("date"),
            func.sum(TokenUsageLog.prompt_tokens).label("prompt_tokens"),
            func.sum(TokenUsageLog.completion_tokens).label("completion_tokens"),
            func.sum(TokenUsageLog.total_tokens).label("total_tokens"),
            func.count(TokenUsageLog.id).label("request_count"),
        )
        .filter(
            TokenUsageLog.user_id == user_id,
            TokenUsageLog.created_at >= since,
        )
        .group_by(func.date(TokenUsageLog.created_at))
        .order_by(func.date(TokenUsageLog.created_at))
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


def get_all_users_token_stats() -> list[dict]:
    """全站用户累计 token 排行（admin）。"""
    users = db.session.query(User).order_by(User.total_tokens.desc()).all()
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
    page: int = 1,
    page_size: int = 20,
    action: str | None = None,
    user_id: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """全站用量明细分页（admin），附带 username。"""
    query = (
        db.session.query(TokenUsageLog, User.username)
        .outerjoin(User, TokenUsageLog.user_id == User.id)
    )

    if user_id:
        query = query.filter(TokenUsageLog.user_id == user_id)

    if action:
        query = query.filter(TokenUsageLog.action == action)

    if start_date:
        try:
            sd = datetime.fromisoformat(start_date)
            query = query.filter(TokenUsageLog.created_at >= sd)
        except ValueError:
            pass

    if end_date:
        try:
            ed = datetime.fromisoformat(end_date)
            query = query.filter(TokenUsageLog.created_at <= ed)
        except ValueError:
            pass

    total = query.count()
    rows = (
        query.order_by(TokenUsageLog.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    items = []
    for log, username in rows:
        d = log.to_dict()
        d["username"] = username or f"user_{log.user_id}"
        items.append(d)

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": items,
    }


def get_all_users_daily_stats(days: int = 30) -> list[dict]:
    """全站合计最近 N 天每日统计（admin）。"""
    since = beijing_now() - timedelta(days=days)
    rows = (
        db.session.query(
            func.date(TokenUsageLog.created_at).label("date"),
            func.sum(TokenUsageLog.prompt_tokens).label("prompt_tokens"),
            func.sum(TokenUsageLog.completion_tokens).label("completion_tokens"),
            func.sum(TokenUsageLog.total_tokens).label("total_tokens"),
            func.count(TokenUsageLog.id).label("request_count"),
        )
        .filter(TokenUsageLog.created_at >= since)
        .group_by(func.date(TokenUsageLog.created_at))
        .order_by(func.date(TokenUsageLog.created_at))
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


def get_per_user_daily_stats(days: int = 30) -> list[dict]:
    """按用户拆分的最近 N 天每日统计（admin）。"""
    since = beijing_now() - timedelta(days=days)
    rows = (
        db.session.query(
            TokenUsageLog.user_id,
            User.username,
            func.date(TokenUsageLog.created_at).label("date"),
            func.sum(TokenUsageLog.prompt_tokens).label("prompt_tokens"),
            func.sum(TokenUsageLog.completion_tokens).label("completion_tokens"),
            func.sum(TokenUsageLog.total_tokens).label("total_tokens"),
            func.count(TokenUsageLog.id).label("request_count"),
        )
        .outerjoin(User, TokenUsageLog.user_id == User.id)
        .filter(TokenUsageLog.created_at >= since)
        .group_by(TokenUsageLog.user_id, User.username, func.date(TokenUsageLog.created_at))
        .order_by(func.date(TokenUsageLog.created_at))
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
