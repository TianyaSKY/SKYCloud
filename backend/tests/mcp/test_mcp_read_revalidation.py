"""基于缓存的协议读取前必须进行重新校验测试."""

import asyncio
from collections.abc import Callable, Coroutine
import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session

from app.mcp import server
from app.mcp.context import McpRequestContext, reset_mcp_context, set_mcp_context
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember


@pytest.mark.parametrize(
    "operation", ["list_files", "get_user_folders", "get_current_user"]
)
def test_removed_member_cannot_read_even_with_cached_content(
    session: Session,
    test_workspace: Workspace,
    test_user: User,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    cached = MagicMock(return_value=[{"id": 1, "name": "PRIVATE_CACHED_FOLDER"}])
    root = MagicMock(return_value=1)
    monkeypatch.setattr(server.folder_service, "get_folders", cached)
    monkeypatch.setattr(server.folder_service, "get_root_folder_id", root)
    monkeypatch.setattr(
        server,
        "SessionLocal",
        lambda: Session(
            bind=session.get_bind(), join_transaction_mode="create_savepoint"
        ),
    )
    member = (
        session.query(WorkspaceMember)
        .filter_by(workspace_id=int(test_workspace.id), user_id=int(test_user.id))
        .one()
    )
    session.delete(member)
    session.flush()
    token = set_mcp_context(
        McpRequestContext(
            user_id=int(test_user.id),
            workspace_id=int(test_workspace.id),
            role="admin",
        )
    )
    try:
        fn: Callable[[], Coroutine[Any, Any, str]] = getattr(server, operation)
        response: str = asyncio.run(fn())
        assert json.loads(response) == {"error": "您不是该工作空间的成员"}
        assert "PRIVATE_CACHED_FOLDER" not in response
        cached.assert_not_called()
        root.assert_not_called()
    finally:
        reset_mcp_context(token)


def test_current_user_reports_current_database_role(
    session: Session,
    test_workspace: Workspace,
    test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member = (
        session.query(WorkspaceMember)
        .filter_by(workspace_id=int(test_workspace.id), user_id=int(test_user.id))
        .one()
    )
    member.role = "viewer"
    session.flush()
    monkeypatch.setattr(
        server,
        "SessionLocal",
        lambda: Session(
            bind=session.get_bind(), join_transaction_mode="create_savepoint"
        ),
    )
    token = set_mcp_context(
        McpRequestContext(
            user_id=int(test_user.id),
            workspace_id=int(test_workspace.id),
            role="admin",
        )
    )
    try:
        raw_response: str = asyncio.run(server.get_current_user())
        response: dict[str, Any] = json.loads(raw_response)
        assert response["workspace_role"] == "viewer"
        assert response["workspace_id"] == test_workspace.id
        assert response["id"] == test_user.id
        assert "password_hash" not in response
    finally:
        reset_mcp_context(token)
