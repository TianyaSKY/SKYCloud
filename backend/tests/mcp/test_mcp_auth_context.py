"""MCP request-context and membership authentication regression tests."""

import asyncio
from unittest.mock import patch

from app.features.auth.service import generate_token
from app.mcp.context import get_mcp_context
from app.mcp.server import JWTAuthMiddleware
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember


def _scope(token: str, workspace_id: int | None = None) -> dict:
    headers = [(b"authorization", f"Bearer {token}".encode())]
    if workspace_id is not None:
        headers.append((b"x-skycloud-workspace-id", str(workspace_id).encode()))
    return {"type": "http", "headers": headers}


def test_mcp_context_uses_workspace_header_and_membership(session, test_user, test_workspace):
    selected_workspace = Workspace(name="second-space", owner_id=test_user.id)
    session.add(selected_workspace)
    session.flush()
    session.add(
        WorkspaceMember(
            workspace_id=selected_workspace.id,
            user_id=test_user.id,
            role="editor",
        )
    )
    session.flush()
    captured = {}

    async def app(scope, receive, send):
        captured["context"] = get_mcp_context()

    middleware = JWTAuthMiddleware(app)
    token = generate_token(test_user.id)

    with patch("app.mcp.server.SessionLocal", return_value=session):
        asyncio.run(middleware(_scope(token, selected_workspace.id), None, None))

    context = captured["context"]
    assert context.user_id == test_user.id
    assert context.workspace_id == selected_workspace.id
    assert context.workspace_id != context.user_id
    assert context.role == "editor"


def test_non_member_cannot_select_workspace(session, test_user, test_workspace):
    outsider = User(username="mcp-outsider", role="common")
    outsider.set_password("password")
    session.add(outsider)
    session.flush()
    outsider_workspace = Workspace(name="outsider-space", owner_id=outsider.id)
    session.add(outsider_workspace)
    session.flush()
    session.add(
        WorkspaceMember(
            workspace_id=outsider_workspace.id,
            user_id=outsider.id,
            role="admin",
        )
    )
    session.flush()

    captured = {}

    async def app(scope, receive, send):
        captured["context"] = None
        try:
            captured["context"] = get_mcp_context()
        except PermissionError:
            pass

    token = generate_token(test_user.id)
    middleware = JWTAuthMiddleware(app)
    with patch("app.mcp.server.SessionLocal", return_value=session):
        asyncio.run(middleware(_scope(token, outsider_workspace.id), None, None))

    assert captured["context"] is None


def test_revoked_mcp_token_is_rejected_immediately(session, test_user, test_workspace):
    from app.mcp.token_service import ensure_user_mcp_token, refresh_user_mcp_token

    _, old_token = ensure_user_mcp_token(session, test_user.id)
    refresh_user_mcp_token(session, test_user.id)

    captured = {}

    async def app(scope, receive, send):
        try:
            captured["context"] = get_mcp_context()
        except PermissionError:
            captured["context"] = None

    middleware = JWTAuthMiddleware(app)
    with patch("app.mcp.server.SessionLocal", return_value=session):
        asyncio.run(middleware(_scope(old_token, test_workspace.id), None, None))

    assert captured["context"] is None
