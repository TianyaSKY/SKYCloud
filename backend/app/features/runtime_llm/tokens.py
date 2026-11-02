"""仅用于模型的短期有效凭证, 按哈希持久化并随运行时一同撤销.

这些凭证仅能对有界模型网关进行鉴权; 绝不携带供应商 API 密钥明文。
"""

import datetime as dt
import os
import uuid
from typing import NotRequired, TypedDict, cast

import jwt
from sqlalchemy.orm import Session

from app.features.workspace.permissions import assert_can_write, check_role_level
from app.infra.datetime_utils import utc_now
from app.infra.extensions import SECRET_KEY
from app.infra.llm.config import get_chat_model_config
from app.mcp.runtime_token_service import validate_bound_token
from app.models.mcp_runtime_token import McpRuntimeToken
from app.models.opencode_runtime import OpenCodeRuntime


class RuntimeLlmClaims(TypedDict):
    """单条运行时模型凭证所携带的经过验证的声明数据."""

    sub: object
    workspace_id: object
    runtime_id: object
    type: str
    jti: object
    role: str
    model: str
    quota: int
    iat: NotRequired[object]
    exp: NotRequired[object]


def positive_setting(name: str, default: int) -> int:
    """读取严格为正整数的配置项."""
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        message = f"{name} must be positive"
        raise ValueError(message)
    return value


def issue_token(session: Session, runtime: OpenCodeRuntime) -> str:
    """在 MCP 刷新撤销旧凭据后签发全新的运行时模型凭证."""
    assert_can_write(session, int(runtime.workspace_id), int(runtime.user_id))
    if runtime.status != "running":
        message = "Runtime must be running"
        raise ValueError(message)
    ttl = min(positive_setting("RUNTIME_LLM_TOKEN_TTL_SECONDS", 3600), 3600)
    now = dt.datetime.now(dt.UTC)
    jti = str(uuid.uuid4())
    payload: dict[str, object] = {
        "sub": str(runtime.user_id),
        "workspace_id": int(runtime.workspace_id),
        "runtime_id": int(runtime.id),
        "type": "runtime_llm",
        "jti": jti,
        "iat": now,
        "exp": now + dt.timedelta(seconds=ttl),
        "model": get_chat_model_config()["model"],
        "quota": positive_setting("RUNTIME_LLM_HOURLY_TOKEN_BUDGET", 100000),
    }
    raw = jwt.encode(payload, SECRET_KEY, algorithm="HS256")
    session.add(
        McpRuntimeToken(
            runtime_id=runtime.id,
            user_id=runtime.user_id,
            workspace_id=runtime.workspace_id,
            jti=jti,
            token_hash=McpRuntimeToken.hash_token(raw),
            token_preview=McpRuntimeToken.preview_token(raw),
            expires_at=utc_now() + dt.timedelta(seconds=ttl),
        ),
    )
    session.commit()
    return raw


def validate_token(
    session: Session,
    raw: str,
    *,
    touch: bool = True,
) -> RuntimeLlmClaims | None:
    """验证签名、撤销状态、成员资格、模型绑定与配额声明."""
    validated = validate_bound_token(
        session,
        raw,
        expected_type="runtime_llm",
        touch=touch,
    )
    if validated is None:
        return None
    payload, _runtime = validated
    role = payload.get("role")
    model = payload.get("model")
    quota = payload.get("quota")
    if not isinstance(role, str) or not check_role_level(role, "editor"):
        return None
    if not isinstance(model, str) or model != get_chat_model_config()["model"]:
        return None
    if type(quota) is not int or quota <= 0:
        return None
    # 保持解码后的全部 JWT 声明 (包括 exp/iat/jti) 不变。
    # 绑定令牌验证器已核验上述身份键与角色, 此处的
    # 检查确立了模型与配额, 无须复制载荷。
    return cast("RuntimeLlmClaims", payload)
