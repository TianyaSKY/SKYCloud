from datetime import datetime
from typing import Any

from fastapi import HTTPException, status

from app.datetime_utils import beijing_now
from app.extensions import db
from app.models.mcp_token import McpToken


def create_mcp_token(
    user_id: int, token: str, expires_at: datetime, name: str | None
) -> McpToken:
    token_record = McpToken(
        user_id=user_id,
        name=(name or "MCP Token").strip() or "MCP Token",
        token_hash=McpToken.hash_token(token),
        token_preview=McpToken.preview_token(token),
        expires_at=expires_at,
    )
    db.session.add(token_record)
    db.session.commit()
    return token_record


def list_mcp_tokens(user_id: int) -> list[dict[str, Any]]:
    tokens = (
        db.session.query(McpToken)
        .filter(McpToken.user_id == user_id)
        .order_by(McpToken.created_at.desc())
        .all()
    )
    return [token.to_dict() for token in tokens]


def revoke_mcp_token(user_id: int, token_id: int) -> McpToken:
    token = (
        db.session.query(McpToken)
        .filter(McpToken.id == token_id, McpToken.user_id == user_id)
        .first()
    )
    if not token:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="MCP token not found"
        )
    if not token.revoked_at:
        token.revoked_at = beijing_now()
        db.session.commit()
    return token


def get_active_mcp_token(token: str) -> McpToken | None:
    token_record = (
        db.session.query(McpToken)
        .filter(McpToken.token_hash == McpToken.hash_token(token))
        .first()
    )
    if not token_record or token_record.is_revoked or token_record.is_expired:
        return None
    token_record.last_used_at = beijing_now()
    db.session.commit()
    return token_record
