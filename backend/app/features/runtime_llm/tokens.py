"""Model-only short lived credentials, persisted by hash and revoked with the runtime."""

import datetime as dt
import os
import uuid

import jwt
from sqlalchemy.orm import Session

from app.features.workspace.permissions import assert_can_write, check_role_level
from app.infra.datetime_utils import beijing_now
from app.infra.extensions import SECRET_KEY
from app.infra.llm.config import get_chat_model_config
from app.mcp.runtime_token_service import validate_bound_token
from app.models.mcp_runtime_token import McpRuntimeToken
from app.models.opencode_runtime import OpenCodeRuntime


def positive_setting(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def issue_token(session: Session, runtime: OpenCodeRuntime) -> str:
    """Called after MCP refresh has revoked all previous runtime credentials."""
    assert_can_write(session, int(runtime.workspace_id), int(runtime.user_id))
    if runtime.status != "running":
        raise ValueError("Runtime must be running")
    ttl = min(positive_setting("RUNTIME_LLM_TOKEN_TTL_SECONDS", 3600), 3600)
    now = dt.datetime.now(dt.timezone.utc)
    jti = str(uuid.uuid4())
    payload = {
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
    session.add(McpRuntimeToken(
        runtime_id=runtime.id, user_id=runtime.user_id, workspace_id=runtime.workspace_id,
        jti=jti, token_hash=McpRuntimeToken.hash_token(raw),
        token_preview=McpRuntimeToken.preview_token(raw),
        expires_at=beijing_now() + dt.timedelta(seconds=ttl),
    ))
    session.commit()
    return raw


def validate_token(session: Session, raw: str) -> dict | None:
    validated = validate_bound_token(session, raw, expected_type="runtime_llm")
    if not validated:
        return None
    payload, _runtime = validated
    if not check_role_level(payload["role"], "editor"):
        return None
    if payload.get("model") != get_chat_model_config()["model"]:
        return None
    if type(payload.get("quota")) is not int or payload["quota"] <= 0:
        return None
    return payload
