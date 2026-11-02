"""请求身份鉴权: 在 worker 线程解析 SQL 作用域并注入请求上下文."""

from collections.abc import Iterable, Mapping
from typing import cast

import jwt
from sqlalchemy.orm import Session
from starlette.types import ASGIApp, Receive, Scope, Send

from app.features.auth.service import decode_token
from app.features.workspace.permissions import get_member_role
from app.infra.extensions import SECRET_KEY, SessionLocal
from app.infra.owned_work import run_owned
from app.mcp import runtime_token_service
from app.mcp.context import (
    McpRequestContext,
    current_user_id,
    reset_mcp_context,
    reset_mcp_request_active,
    reset_request_id,
    set_mcp_context,
    set_mcp_request_active,
    set_request_id,
)
from app.mcp.unit_of_work import worker_session

_AUTHORIZATION_HEADER = b"authorization"
_WORKSPACE_HEADER = b"x-skycloud-workspace-id"
_REQUEST_ID_HEADER = b"x-request-id"
_ASGI_HTTP_SCOPES = frozenset({"http", "websocket"})


def _request_headers(scope: Scope) -> dict[bytes, bytes]:
    """将 ASGI 请求头转为小写以实现大小写无关的检索."""
    raw_headers = cast("Iterable[tuple[bytes, bytes]]", scope.get("headers", []))
    return {key.lower(): value for key, value in raw_headers}


def _header_text(headers: Mapping[bytes, bytes], name: bytes) -> str | None:
    value = headers.get(name)
    if value is None:
        return None
    return value.decode("utf-8", errors="ignore")


def _int_or_none(value: object) -> int | None:
    """解析数值型声明或请求头, 拒绝任何其他非法格式."""
    if not isinstance(value, (int, float, str)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _workspace_matches(workspace_header: str | None, workspace_id: int) -> bool:
    """根据令牌携带的工作区校验可选的工作区请求头."""
    if workspace_header is None:
        return True
    parsed = _int_or_none(workspace_header)
    return parsed is not None and parsed == workspace_id


class JWTAuthMiddleware:
    """为每个 MCP 请求解析经过校验的用户与工作区上下文.

    常规 session/MCP 令牌通过 ``X-SKYCloud-Workspace-Id`` 和成员查询选择工作区。
    运行时令牌在其签名声明中携带工作区, 但仍会在数据库中检查运行时与当前成员资格。
    """

    def __init__(self, app: ASGIApp) -> None:
        """包装下游 ASGI 应用程序."""
        self.app = app

    @staticmethod
    def _resolve_context(
        session: Session,
        token: str,
        workspace_header: str | None,
    ) -> McpRequestContext | None:
        try:
            payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        except jwt.InvalidTokenError:
            return None

        if payload.get("type") == "mcp_runtime":
            return JWTAuthMiddleware._resolve_runtime_context(
                session,
                token,
                workspace_header,
            )
        return JWTAuthMiddleware._resolve_session_context(
            session,
            token,
            workspace_header,
        )

    @staticmethod
    def _resolve_runtime_context(
        session: Session,
        token: str,
        workspace_header: str | None,
    ) -> McpRequestContext | None:
        validated = runtime_token_service.validate_runtime_token(session, token)
        if not validated:
            return None
        runtime_payload, runtime = validated
        workspace_id = _int_or_none(runtime_payload.get("workspace_id"))
        if workspace_id is None or not _workspace_matches(
            workspace_header,
            workspace_id,
        ):
            return None
        return McpRequestContext(
            user_id=int(runtime.user_id),
            workspace_id=workspace_id,
            role=str(runtime_payload.get("role", "viewer")),
            runtime_id=int(runtime.id),
            actor_type="opencode",
        )

    @staticmethod
    def _resolve_session_context(
        session: Session,
        token: str,
        workspace_header: str | None,
    ) -> McpRequestContext | None:
        result = decode_token(session, token)
        if not result or not str(result).isdigit() or workspace_header is None:
            return None
        user_id = _int_or_none(result)
        workspace_id = _int_or_none(workspace_header)
        if user_id is None or workspace_id is None:
            return None

        role = get_member_role(session, workspace_id, user_id)
        if role is None:
            return None
        return McpRequestContext(
            user_id=user_id,
            workspace_id=workspace_id,
            role=role.value,
            actor_type="user",
        )

    @staticmethod
    def _authenticate(
        token: str,
        workspace_header: str | None,
    ) -> tuple[McpRequestContext | None, int | None]:
        with worker_session(SessionLocal) as session:
            resolved = JWTAuthMiddleware._resolve_context(
                session,
                token,
                workspace_header,
            )
            if resolved:
                return resolved, resolved.user_id
            # 当无法解析出已验证的工作区上下文时,
            # 诊断身份绝不能为真实工具调用进行授权。
            result = decode_token(session, token)
            return None, int(result) if result and str(result).isdigit() else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """对单个 ASGI 请求进行鉴权, 注入并随后重置其上下文."""
        if scope["type"] not in _ASGI_HTTP_SCOPES:
            await self.app(scope, receive, send)
            return

        headers = _request_headers(scope)
        auth_value = _header_text(headers, _AUTHORIZATION_HEADER) or ""
        workspace_header_value = _header_text(headers, _WORKSPACE_HEADER)

        user_id: int | None = None
        context: McpRequestContext | None = None

        if auth_value.lower().startswith("bearer "):
            token = auth_value.split(" ", 1)[1].strip()
            if token:
                context, user_id = await run_owned(
                    self._authenticate,
                    token,
                    workspace_header_value,
                )

        request_active_ctx = set_mcp_request_active(active=True)
        token_ctx = current_user_id.set(user_id)
        context_ctx = set_mcp_context(context)
        request_id_ctx = set_request_id(
            _header_text(headers, _REQUEST_ID_HEADER) or None,
        )
        try:
            await self.app(scope, receive, send)
        finally:
            reset_request_id(request_id_ctx)
            reset_mcp_context(context_ctx)
            current_user_id.reset(token_ctx)
            reset_mcp_request_active(request_active_ctx)
