"""签发并校验短期有效 OpenCode MCP 运行时令牌."""

import datetime as _dt
import os
import uuid
from dataclasses import dataclass
from typing import Any

import jwt
from sqlalchemy.orm import Session

from app.features.workspace.permissions import get_member_role
from app.infra.datetime_utils import utc_now
from app.infra.extensions import SECRET_KEY
from app.models.mcp_runtime_token import McpRuntimeToken
from app.models.opencode_runtime import OpenCodeRuntime

DEFAULT_RUNTIME_TOKEN_TTL_SECONDS = 60 * 60
_MIN_TTL_SECONDS = 60


@dataclass(frozen=True, slots=True, kw_only=True)
class GenerateRuntimeTokenParams:
    """生成运行时令牌所需的签名声明数据."""

    user_id: int
    workspace_id: int
    runtime_id: int
    role: str
    jti: str
    expires_at: _dt.datetime


@dataclass(frozen=True, slots=True)
class BoundClaims:
    """解码后的声明及必须匹配的身份字段."""

    payload: dict[str, Any]
    user_id: int
    workspace_id: int
    runtime_id: int
    jti: str


def _ttl_seconds(value: int | None = None) -> int:
    if value is not None:
        return max(_MIN_TTL_SECONDS, int(value))
    try:
        return max(
            _MIN_TTL_SECONDS,
            int(
                os.getenv(
                    "MCP_RUNTIME_TOKEN_TTL_SECONDS",
                    str(DEFAULT_RUNTIME_TOKEN_TTL_SECONDS),
                ),
            ),
        )
    except (TypeError, ValueError):
        return DEFAULT_RUNTIME_TOKEN_TTL_SECONDS


def _generate_runtime_token(params: GenerateRuntimeTokenParams) -> str:
    payload = {
        "exp": params.expires_at,
        "iat": _dt.datetime.now(_dt.UTC),
        "sub": str(params.user_id),
        "type": "mcp_runtime",
        "workspace_id": int(params.workspace_id),
        "runtime_id": int(params.runtime_id),
        "role": params.role,
        "jti": params.jti,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")


def issue_runtime_token(
    session: Session,
    runtime: OpenCodeRuntime,
    *,
    role: str,
    ttl_seconds: int | None = None,
) -> tuple[McpRuntimeToken, str]:
    """撤销此前的运行时令牌, 并签发全新的单条令牌."""
    now = utc_now()
    session.query(McpRuntimeToken).filter(
        McpRuntimeToken.runtime_id == runtime.id,
        McpRuntimeToken.revoked_at.is_(None),
    ).update({McpRuntimeToken.revoked_at: now}, synchronize_session="fetch")

    ttl = _ttl_seconds(ttl_seconds)
    expires_at = now + _dt.timedelta(seconds=ttl)
    jwt_expires_at = _dt.datetime.now(_dt.UTC) + _dt.timedelta(seconds=ttl)
    jti = str(uuid.uuid4())
    raw_token = _generate_runtime_token(
        GenerateRuntimeTokenParams(
            user_id=int(runtime.user_id),
            workspace_id=int(runtime.workspace_id),
            runtime_id=int(runtime.id),
            role=role,
            jti=jti,
            expires_at=jwt_expires_at,
        ),
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
    """轮转运行时令牌; 等同于重新签发全新令牌."""
    return issue_runtime_token(
        session,
        runtime,
        role=role,
        ttl_seconds=ttl_seconds,
    )


def get_active_runtime_token(
    session: Session,
    token: str,
    *,
    touch: bool = True,
) -> McpRuntimeToken | None:
    """通过哈希查找原始令牌, 并拒绝已撤销或已过期的记录."""
    record = (
        session.query(McpRuntimeToken)
        .filter(McpRuntimeToken.token_hash == McpRuntimeToken.hash_token(token))
        .first()
    )
    if not record or not record.is_active:
        return None
    if touch:
        record.last_used_at = utc_now()
        session.commit()
    return record


def _decode_bound_claims(token: str, expected_type: str) -> BoundClaims | None:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
    except jwt.InvalidTokenError:
        return None

    if payload.get("type") != expected_type:
        return None

    try:
        return BoundClaims(
            payload=payload,
            user_id=int(payload["sub"]),
            workspace_id=int(payload["workspace_id"]),
            runtime_id=int(payload["runtime_id"]),
            jti=str(payload["jti"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _record_matches(record: McpRuntimeToken, claims: BoundClaims) -> bool:
    return (
        int(record.user_id) == claims.user_id
        and int(record.workspace_id) == claims.workspace_id
        and int(record.runtime_id) == claims.runtime_id
        and record.jti == claims.jti
    )


def _runtime_matches(runtime: OpenCodeRuntime, claims: BoundClaims) -> bool:
    return (
        int(runtime.user_id) == claims.user_id
        and int(runtime.workspace_id) == claims.workspace_id
    )


def validate_runtime_token(
    session: Session,
    token: str,
) -> tuple[dict[str, Any], OpenCodeRuntime] | None:
    """验证 MCP 凭证; 模型凭据不可用于工具调用."""
    return validate_bound_token(session, token, expected_type="mcp_runtime")


def validate_bound_token(
    session: Session,
    token: str,
    *,
    expected_type: str,
    touch: bool = True,
) -> tuple[dict[str, Any], OpenCodeRuntime] | None:
    """共享的签名核验、数据库撤销状态、绑定关系与成员资格检查."""
    claims = _decode_bound_claims(token, expected_type)
    if claims is None:
        return None

    record = get_active_runtime_token(session, token, touch=touch)
    if not record or not _record_matches(record, claims):
        return None

    runtime = session.get(OpenCodeRuntime, claims.runtime_id)
    if not runtime or not _runtime_matches(runtime, claims):
        return None
    if runtime.status != "running":
        return None

    member_role = get_member_role(
        session,
        claims.workspace_id,
        claims.user_id,
    )
    if member_role is None:
        return None

    # role 声明仅供参考; 鉴权始终使用数据库中的当前角色。
    claims.payload["role"] = member_role.value
    return claims.payload, runtime


def revoke_runtime_tokens(session: Session, runtime_id: int) -> int:
    """撤销指定运行时的所有有效令牌, 并返回受影响的行数."""
    result = session.query(McpRuntimeToken).filter(
        McpRuntimeToken.runtime_id == runtime_id,
        McpRuntimeToken.revoked_at.is_(None),
    ).update({McpRuntimeToken.revoked_at: utc_now()}, synchronize_session="fetch")
    return int(result or 0)


def revoke_workspace_runtime_tokens(session: Session, workspace_id: int) -> int:
    """撤销指定工作区下的所有有效运行时令牌."""
    result = session.query(McpRuntimeToken).filter(
        McpRuntimeToken.workspace_id == workspace_id,
        McpRuntimeToken.revoked_at.is_(None),
    ).update({McpRuntimeToken.revoked_at: utc_now()}, synchronize_session="fetch")
    return int(result or 0)
