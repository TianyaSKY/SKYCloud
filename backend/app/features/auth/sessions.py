"""Validate Web session versions against authoritative account state."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.user import User


def valid_session(session: Session, payload: dict) -> bool:
    version = payload.get("ver")
    if type(version) is not int or version < 0:
        return False
    try:
        user_id = int(payload["sub"])
    except (KeyError, TypeError, ValueError):
        return False
    if user_id <= 0:
        return False
    # A scalar SELECT bypasses profile caches and existing ORM identity state.
    current = session.scalar(select(User.auth_version).where(User.id == user_id))
    return current is not None and current == version
