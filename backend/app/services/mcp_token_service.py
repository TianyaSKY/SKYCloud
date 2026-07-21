"""用户 MCP Token 生命周期：保证「有且只有一个」有效 Token，并支持鉴权校验与旧接口兼容。"""

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.exceptions import ServiceOperationError
from app.infra.datetime_utils import beijing_now
from app.models.mcp_token import McpToken


def _issue_jwt(user_id: int, expires_at: datetime) -> str:
    from app.services.auth_service import generate_mcp_token

    token = generate_mcp_token(user_id, expires_at)
    if not token:
        raise ServiceOperationError("Failed to generate MCP token")
    return token


def _revoke_all_active(session: Session, user_id: int) -> None:
    """撤销用户所有未撤销的 Token，保证「有且只有一个」。"""
    now = beijing_now()
    (
        session.query(McpToken)
        .filter(McpToken.user_id == user_id, McpToken.revoked_at.is_(None))
        .update({McpToken.revoked_at: now}, synchronize_session=False)
    )


def _create_token_record(
    session: Session, user_id: int, token: str, expires_at: datetime, name: str = "MCP Token"
) -> McpToken:
    record = McpToken(
        user_id=user_id,
        name=(name or "MCP Token").strip() or "MCP Token",
        token_hash=McpToken.hash_token(token),
        token_preview=McpToken.preview_token(token),
        token_value=token,
        expires_at=expires_at,
    )
    session.add(record)
    return record


def get_active_record(session: Session, user_id: int) -> McpToken | None:
    """返回用户当前有效的 MCP Token（未撤销且未过期）；历史多条时只保留最新。"""
    now = beijing_now()
    records = (
        session.query(McpToken)
        .filter(
            McpToken.user_id == user_id,
            McpToken.revoked_at.is_(None),
            McpToken.expires_at > now,
        )
        .order_by(McpToken.created_at.desc())
        .all()
    )
    if not records:
        return None
    # 历史多 Token：只保留最新一条，其余撤销
    active = records[0]
    if len(records) > 1:
        for extra in records[1:]:
            extra.revoked_at = now
        session.commit()
    return active


def ensure_user_mcp_token(session: Session, user_id: int) -> tuple[McpToken, str]:
    """确保用户有且只有一个有效 MCP Token，返回 (record, raw_jwt)。

    若已有有效记录且 token_value 可用则复用；否则签发新 Token。
    """
    active = get_active_record(session, user_id)
    if active and active.token_value:
        return active, active.token_value

    # 无有效 Token，或历史数据缺少 token_value → 重新签发
    return refresh_user_mcp_token(session, user_id)


def refresh_user_mcp_token(session: Session, user_id: int) -> tuple[McpToken, str]:
    """撤销旧 Token 并签发唯一新 Token。"""
    jwt_expires = datetime.now(timezone.utc) + timedelta(days=365)
    db_expires = beijing_now() + timedelta(days=365)
    token = _issue_jwt(user_id, jwt_expires)

    _revoke_all_active(session, user_id)
    record = _create_token_record(session, user_id, token, db_expires)
    session.commit()
    return record, token


def get_user_mcp_token_payload(session: Session, user_id: int) -> dict[str, Any]:
    """组装 API 响应：完整 Token + 元数据。"""
    record, raw = ensure_user_mcp_token(session, user_id)
    return {
        "mcp_token": raw,
        "token": record.to_dict(),
        "user_id": user_id,
        "expires_in_days": 365,
        "usage": "Set as Authorization header: Bearer <mcp_token>",
    }


def get_active_mcp_token(session: Session, token: str) -> McpToken | None:
    """按完整 JWT 校验是否为有效 MCP Token（鉴权用）；命中则更新 last_used_at。"""
    token_record = (
        session.query(McpToken)
        .filter(McpToken.token_hash == McpToken.hash_token(token))
        .first()
    )
    if not token_record or token_record.is_revoked or token_record.is_expired:
        return None
    token_record.last_used_at = beijing_now()
    session.commit()
    return token_record


# ---------------------------------------------------------------------------
# 兼容旧调用（工作区 / 历史接口）
# ---------------------------------------------------------------------------


def create_mcp_token(
    session: Session,
    user_id: int,
    token: str,
    expires_at: datetime,
    name: str | None,
) -> McpToken:
    """兼容旧签名：创建 Token 前先撤销用户其它有效 Token。"""
    _revoke_all_active(session, user_id)
    record = _create_token_record(
        session, user_id, token, expires_at, name or "MCP Token"
    )
    session.commit()
    return record
