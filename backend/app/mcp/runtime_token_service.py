"""Issuance and validation of short-lived OpenCode MCP runtime tokens."""

import datetime as _dt
import os
import uuid
from typing import Any

import jwt
from sqlalchemy.orm import Session

from app.features.workspace.permissions import get_member_role
from app.infra.datetime_utils import beijing_now
from app.infra.extensions import SECRET_KEY
from app.models.mcp_runtime_token import McpRuntimeToken
from app.models.opencode_runtime import OpenCodeRuntime


DEFAULT_RUNTIME_TOKEN_TTL_SECONDS = 60 * 60


def _ttl_seconds(value: int | None = None) -> int:
    if value is not None:
        return max(60, int(value))
    try:
        return max(
            60,
            int(
                os.getenv(
                    "MCP_RUNTIME_TOKEN_TTL_SECONDS",
                    str(DEFAULT_RUNTIME_TOKEN_TTL_SECONDS),
                )
            ),
        )
    except (TypeError, ValueError):
        return DEFAULT_RUNTIME_TOKEN_TTL_SECONDS


def _generate_runtime_token(
    *,
    user_id: int,
    workspace_id: int,
    runtime_id: int,
    role: str,
    jti: str,
    expires_at: _dt.datetime,
) -> str:
    issued_at = _dt.datetime.now(_dt.timezone.utc)
    payload = {
        "exp": expires_at,
        "iat": issued_at,
        "sub": str(user_id),
        "type": "mcp_runtime",
        "workspace_id": int(workspace_id),
        "runtime_id": int(runtime_id),
        "role": role,
        "jti": jti,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")


def issue_runtime_token(
    session: Session,
    runtime: OpenCodeRuntime,
    *,
    role: str,
    ttl_seconds: int | None = None,
) -> tuple[McpRuntimeToken, str]:
    """Revoke prior runtime tokens and issue exactly one new token."""

    now = beijing_now()
    session.query(McpRuntimeToken).filter(
        McpRuntimeToken.runtime_id == runtime.id,
        McpRuntimeToken.revoked_at.is_(None),
    ).update({McpRuntimeToken.revoked_at: now}, synchronize_session="fetch")

    ttl = _ttl_seconds(ttl_seconds)
    expires_at = now + _dt.timedelta(seconds=ttl)
    jwt_expires_at = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=ttl)
    jti = str(uuid.uuid4())
    raw_token = _generate_runtime_token(
        user_id=int(runtime.user_id),
        workspace_id=int(runtime.workspace_id),
        runtime_id=int(runtime.id),
        role=role,
        jti=jti,
        expires_at=jwt_expires_at,
    )
    record = McpRuntimeToken(
        runtime_id=runtime.id,
        user_id=runtime.user_id,
        workspace_id=runtime.workspace_id,
        jti=jti,
        token_hash=McpRuntimeToken.hash_token(raw_token),
        token_preview=McpRuntimeToken.preview_token(raw_token),
        expires_at=expires_at,
    )
    session.add(record)
    session.commit()
    return record, raw_token


def refresh_runtime_token(
    session: Session,
    runtime: OpenCodeRuntime,
    *,
    role: str,
    ttl_seconds: int | None = None,
) -> tuple[McpRuntimeToken, str]:
    return issue_runtime_token(
        session,
        runtime,
        role=role,
        ttl_seconds=ttl_seconds,
    )


def get_active_runtime_token(session: Session, token: str) -> McpRuntimeToken | None:
    """Look up the raw token by hash and reject revoked/expired records."""

    record = (
        session.query(McpRuntimeToken)
        .filter(McpRuntimeToken.token_hash == McpRuntimeToken.hash_token(token))
        .first()
    )
    if not record or not record.is_active:
        return None
    record.last_used_at = beijing_now()
    session.commit()
    return record


def validate_runtime_token(
    session: Session, token: str
) -> tuple[dict[str, Any], OpenCodeRuntime] | None:
    """Verify signature, database revocation, runtime binding, and membership."""

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
    except jwt.InvalidTokenError:
        return None

    if payload.get("type") != "mcp_runtime":
        return None

    try:
        user_id = int(payload["sub"])
        workspace_id = int(payload["workspace_id"])
        runtime_id = int(payload["runtime_id"])
        jti = str(payload["jti"])
    except (KeyError, TypeError, ValueError):
        return None

    record = get_active_runtime_token(session, token)
    if not record:
        return None
    if (
        int(record.user_id) != user_id
        or int(record.workspace_id) != workspace_id
        or int(record.runtime_id) != runtime_id
        or record.jti != jti
    ):
        return None

    runtime = session.get(OpenCodeRuntime, runtime_id)
    if not runtime or int(runtime.user_id) != user_id or int(runtime.workspace_id) != workspace_id:
        return None
    if runtime.status != "running":
        return None

    member_role = get_member_role(session, workspace_id, user_id)
    if member_role is None:
        return None

    # The role claim is informational; authorization always uses current DB role.
    payload["role"] = member_role.value
    return payload, runtime


def revoke_runtime_tokens(session: Session, runtime_id: int) -> int:
    """Revoke all active tokens for a runtime and return the affected count."""

    result = session.query(McpRuntimeToken).filter(
        McpRuntimeToken.runtime_id == runtime_id,
        McpRuntimeToken.revoked_at.is_(None),
    ).update({McpRuntimeToken.revoked_at: beijing_now()}, synchronize_session="fetch")
    return int(result or 0)


def revoke_workspace_runtime_tokens(session: Session, workspace_id: int) -> int:
    result = session.query(McpRuntimeToken).filter(
        McpRuntimeToken.workspace_id == workspace_id,
        McpRuntimeToken.revoked_at.is_(None),
    ).update({McpRuntimeToken.revoked_at: beijing_now()}, synchronize_session="fetch")
    return int(result or 0)
