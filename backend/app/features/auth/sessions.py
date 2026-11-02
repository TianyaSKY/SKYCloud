"""针对权威账户状态核验 Web 会话版本有效性."""

from collections.abc import Mapping
from typing import cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.features.auth import refresh_sessions
from app.models.user import User


def valid_session(session: Session, payload: Mapping[str, object]) -> bool:
    """返回载荷中的会话版本是否与数据库账户记录一致."""
    version = payload.get("ver")
    if type(version) is not int or version < 0:
        return False
    try:
        user_id = int(cast("str | int", payload["sub"]))
    except (KeyError, TypeError, ValueError):
        return False
    if user_id <= 0:
        return False
    if "sid" in payload and not refresh_sessions.access_active(payload):
        return False
    # 标量 SELECT 查询绕过画像缓存与现有 ORM 身份状态。
    current = session.scalar(select(User.auth_version).where(User.id == user_id))
    return current is not None and current == version
