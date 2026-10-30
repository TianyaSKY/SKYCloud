"""Validate Web session versions against authoritative account state."""

from collections.abc import Mapping
from typing import cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.features.auth import refresh_sessions
from app.models.user import User


def valid_session(session: Session, payload: Mapping[str, object]) -> bool:
    """Return whether the payload's session version matches the account row."""
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
    # A scalar SELECT bypasses profile caches and existing ORM identity state.
    current = session.scalar(select(User.auth_version).where(User.id == user_id))
    return current is not None and current == version
