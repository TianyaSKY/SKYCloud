"""MCP 请求上下文与成员认证回归测试."""

import asyncio
from typing import Any
from unittest.mock import patch

from starlette.types import Message, Receive, Scope, Send
from sqlalchemy.orm import Session

from app.features.auth.service import generate_token
from app.mcp.context import McpRequestContext, get_mcp_context
from app.mcp.server import JWTAuthMiddleware
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember


def _scope(token: str, workspace_id: int | None = None) -> Scope:
    headers: list[tuple[bytes, bytes]] = [(b"authorization", f"Bearer {token}".encode())]
    if workspace_id is not None:
        headers.append((b"x-skycloud-workspace-id", str(workspace_id).encode()))
    return {"type": "http", "headers": headers}  # pyright: ignore[reportReturnType]


async def _dummy_receive() -> Message:
    return {"type": "http.request"}


async def _dummy_send(message: Message) -> None:
    pass


def test_mcp_context_uses_workspace_header_and_membership(
    session: Session, test_user: User, test_workspace: Workspace
) -> None:
    selected_workspace = Workspace(name="second-space", owner_id=int(test_user.id))
    session.add(selected_workspace)
    session.flush()
    session.add(
        WorkspaceMember(
            workspace_id=int(selected_workspace.id),
            user_id=int(test_user.id),
            role="editor",
        )
    )
    session.flush()
    captured: dict[str, Any] = {}

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        captured["context"] = get_mcp_context()

    middleware = JWTAuthMiddleware(app)
    token = generate_token(int(test_user.id))
    assert token is not None

    with patch("app.mcp.authentication.SessionLocal", return_value=session):
        asyncio.run(
            middleware(_scope(token, int(selected_workspace.id)), _dummy_receive, _dummy_send)
        )

    context: McpRequestContext = captured["context"]
    assert context.user_id == test_user.id
    assert context.workspace_id == selected_workspace.id
    assert context.workspace_id != context.user_id
    assert context.role == "editor"


def test_non_member_cannot_select_workspace(
    session: Session, test_user: User, test_workspace: Workspace
) -> None:
    outsider = User(username="mcp-outsider", role="common")
    outsider.set_password("password")
    session.add(outsider)
    session.flush()
    outsider_workspace = Workspace(name="outsider-space", owner_id=int(outsider.id))
    session.add(outsider_workspace)
    session.flush()
    session.add(
        WorkspaceMember(
            workspace_id=int(outsider_workspace.id),
            user_id=int(outsider.id),
            role="admin",
        )
    )
    session.flush()

    captured: dict[str, Any] = {}

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        captured["context"] = None
        try:
            captured["context"] = get_mcp_context()
        except PermissionError:
            pass

    token = generate_token(int(test_user.id))
    assert token is not None
    middleware = JWTAuthMiddleware(app)
    with patch("app.mcp.authentication.SessionLocal", return_value=session):
        asyncio.run(
            middleware(_scope(token, int(outsider_workspace.id)), _dummy_receive, _dummy_send)
        )

    assert captured["context"] is None


def test_revoked_mcp_token_is_rejected_immediately(
    session: Session, test_user: User, test_workspace: Workspace
) -> None:
    from app.mcp.token_service import ensure_user_mcp_token, refresh_user_mcp_token

    _, old_token = ensure_user_mcp_token(session, int(test_user.id))
    assert old_token is not None
    refresh_user_mcp_token(session, int(test_user.id))

    captured: dict[str, Any] = {}

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        try:
            captured["context"] = get_mcp_context()
        except PermissionError:
            captured["context"] = None

    middleware = JWTAuthMiddleware(app)
    with patch("app.mcp.authentication.SessionLocal", return_value=session):
        asyncio.run(
            middleware(_scope(old_token, int(test_workspace.id)), _dummy_receive, _dummy_send)
        )

    assert captured["context"] is None
