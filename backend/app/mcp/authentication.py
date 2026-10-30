"""Request authentication: resolve SQL scope on a worker, then install context."""

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
    """Lower-case the ASGI header names for case-insensitive lookup."""
    raw_headers = cast("Iterable[tuple[bytes, bytes]]", scope.get("headers", []))
    return {key.lower(): value for key, value in raw_headers}


def _header_text(headers: Mapping[bytes, bytes], name: bytes) -> str | None:
    value = headers.get(name)
    if value is None:
        return None
    return value.decode("utf-8", errors="ignore")


def _int_or_none(value: object) -> int | None:
    """Parse a numeric claim/header value, rejecting anything else."""
    if not isinstance(value, (int, float, str)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _workspace_matches(workspace_header: str | None, workspace_id: int) -> bool:
    """Check the optional header against the workspace carried by the token."""
    if workspace_header is None:
        return True
    parsed = _int_or_none(workspace_header)
    return parsed is not None and parsed == workspace_id


class JWTAuthMiddleware:
    """Resolve a verified user + workspace context for every MCP request.

    A normal session/MCP token selects a workspace through
    ``X-SKYCloud-Workspace-Id`` and membership lookup. A runtime token carries
    its workspace in signed claims, but the runtime and current membership are
    still checked in the database.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap the downstream ASGI application."""
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
            # Diagnostic identity never authorizes a real tool when
            # no verified workspace context could be resolved.
            result = decode_token(session, token)
            return None, int(result) if result and str(result).isdigit() else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Authenticate one ASGI request, then install and reset its context."""
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
