"""Token 使用量记录与查询服务"""
import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy import func, text

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
    """记录一次 token 使用，并同步更新 users 表的累计统计。

    使用独立 session 以避免干扰调用方事务。
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

        # 原子更新 users 表累计统计
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
                "now": datetime.now(timezone.utc),
            },
        )
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("Failed to record token usage for user %s", user_id)
    finally:
        session.close()


def get_user_token_stats(user_id: int) -> dict:
    """获取用户的累计 token 使用统计"""
    user = db.session.get(User, user_id)
    if not user:
        return {}
    return {
        "user_id": user_id,
        "total_prompt_tokens": user.total_prompt_tokens or 0,
        "total_completion_tokens": user.total_completion_tokens or 0,
        "total_tokens": user.total_tokens or 0,
        "last_active_at": user.last_active_at.isoformat() if user.last_active_at else None,
    }


def get_usage_logs(
    user_id: int,
    page: int = 1,
    page_size: int = 20,
    action: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """分页查询用户的 token 使用明细"""
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
    """获取用户最近 N 天的每日 token 使用统计"""
    since = datetime.now(timezone.utc) - timedelta(days=days)
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
