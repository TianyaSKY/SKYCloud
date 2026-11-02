"""使用工作线程持有的 SQL 单元与游离用户数据解析 HTTP 凭证."""

from collections.abc import Mapping
from typing import cast

from fastapi import HTTPException

from app.api.stream_credentials import StreamCredential
from app.features.auth import account_repository, sessions, user_service
from app.infra.extensions import SessionFactory
from app.mcp import token_service
from app.models.user import User, UserDict

_MCP_REVOKED = "MCP token is revoked or expired!"
_SESSION_REVOKED = "Session revoked. Please log in again."
_USER_MISSING = "User not found!"


def resolve_http_user(
    session_factory: SessionFactory,
    payload: Mapping[str, object],
    token: str,
) -> User:
    """在调用方的工作线程中将令牌解析为游离的 User 对象."""
    with session_factory() as session:
        if payload.get("type") == "mcp":
            if not token_service.get_active_mcp_token(session, token):
                raise HTTPException(status_code=401, detail=_MCP_REVOKED)
        elif not sessions.valid_session(session, payload):
            raise HTTPException(status_code=401, detail=_SESSION_REVOKED)
        user_id = int(cast("str | int", payload["sub"]))
        role = account_repository.current_role(session, user_id)
        data = user_service.get_user_data(session, user_id)
        if role is None or not data:
            raise HTTPException(status_code=401, detail=_USER_MISSING)
        # 不修改缓存的用户档案数据。鉴权角色始终来自 SQL 查询。
        user = User.from_cache(cast("UserDict", {**data, "role": role}))
        # 仅请求局部的游离对象持有原始凭据。
        # 绝不放入缓存的用户档案数据或 User.to_dict() 中。
        user.http_stream_credential = StreamCredential(token, user_id)
        return user
