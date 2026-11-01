"""Revalidation must precede cache-backed protocol reads."""
import asyncio
import json
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session

from app.mcp import server
from app.mcp.context import McpRequestContext, set_mcp_context, reset_mcp_context
from app.models.workspace import WorkspaceMember


@pytest.mark.parametrize("operation", ["list_files","get_user_folders","get_current_user"])
def test_removed_member_cannot_read_even_with_cached_content(
        session,test_workspace,test_user,monkeypatch,operation):
    cached = MagicMock(return_value=[{"id":1,"name":"PRIVATE_CACHED_FOLDER"}])
    root = MagicMock(return_value=1)
    monkeypatch.setattr(server.folder_service,"get_folders",cached)
    monkeypatch.setattr(server.folder_service,"get_root_folder_id",root)
    monkeypatch.setattr(server,"SessionLocal",lambda: Session(
        bind=session.get_bind(),join_transaction_mode="create_savepoint"))
    member = session.query(WorkspaceMember).filter_by(workspace_id=test_workspace.id,user_id=test_user.id).one()
    session.delete(member)
    session.flush()
    token = set_mcp_context(McpRequestContext(user_id=test_user.id,workspace_id=test_workspace.id,role="admin"))
    try:
        response = asyncio.run(getattr(server,operation)())
        assert json.loads(response) == {"error":"您不是该工作空间的成员"}
        assert "PRIVATE_CACHED_FOLDER" not in response
        cached.assert_not_called()
        root.assert_not_called()
    finally:
        reset_mcp_context(token)


def test_current_user_reports_current_database_role(session,test_workspace,test_user,monkeypatch):
    member = session.query(WorkspaceMember).filter_by(workspace_id=test_workspace.id,user_id=test_user.id).one()
    member.role = "viewer"
    session.flush()
    monkeypatch.setattr(server,"SessionLocal",lambda: Session(
        bind=session.get_bind(),join_transaction_mode="create_savepoint"))
    token = set_mcp_context(McpRequestContext(user_id=test_user.id,workspace_id=test_workspace.id,role="admin"))
    try:
        response = json.loads(asyncio.run(server.get_current_user()))
        assert response["workspace_role"] == "viewer"
        assert response["workspace_id"] == test_workspace.id
        assert response["id"] == test_user.id
        assert "password_hash" not in response
    finally:
        reset_mcp_context(token)

