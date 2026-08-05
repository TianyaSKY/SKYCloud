"""MCP role and cross-uploader authorization regression tests."""

import asyncio
import json

import pytest

from app.mcp.context import McpRequestContext, reset_mcp_context, set_mcp_context
from app.mcp.server import create_folder, get_file_info, move_file
from app.models.file import File
from app.models.user import User
from app.models.workspace import WorkspaceMember


def _context(user_id: int, workspace_id: int, role: str):
    return set_mcp_context(
        McpRequestContext(user_id=user_id, workspace_id=workspace_id, role=role)
    )


def test_viewer_can_read_but_cannot_create_folder(session, test_user, test_workspace):
    member = session.query(WorkspaceMember).filter_by(
        workspace_id=test_workspace.id, user_id=test_user.id
    ).one()
    member.role = "viewer"
    session.flush()
    context_token = _context(test_user.id, test_workspace.id, "viewer")
    try:
        with pytest.raises(PermissionError):
            asyncio.run(create_folder("forbidden"))
    finally:
        reset_mcp_context(context_token)


def test_editor_can_update_file_uploaded_by_another_member(session, test_user, test_workspace):
    other = User(username="other-uploader", role="common")
    other.set_password("password")
    session.add(other)
    session.flush()
    session.add(
        WorkspaceMember(
            workspace_id=test_workspace.id,
            user_id=other.id,
            role="viewer",
        )
    )
    file_obj = File(
        name="shared.txt",
        file_path="shared.txt",
        file_size=10,
        mime_type="text/plain",
        workspace_id=test_workspace.id,
        uploader_id=other.id,
        status="success",
    )
    session.add(file_obj)
    session.flush()

    context_token = _context(test_user.id, test_workspace.id, "editor")
    try:
        from unittest.mock import patch

        with patch("app.mcp.server.SessionLocal", return_value=session):
            result = json.loads(
                asyncio.run(move_file(file_obj.id, new_name="renamed-by-editor.txt"))
            )
        assert result["name"] == "renamed-by-editor.txt"
    finally:
        reset_mcp_context(context_token)


def test_member_can_read_file_from_workspace_even_when_not_uploader(
    session, test_user, test_workspace
):
    other = User(username="reader-uploader", role="common")
    other.set_password("password")
    session.add(other)
    session.flush()
    session.add(
        WorkspaceMember(
            workspace_id=test_workspace.id,
            user_id=other.id,
            role="viewer",
        )
    )
    file_obj = File(
        name="member-file.txt",
        file_path="member-file.txt",
        file_size=10,
        mime_type="text/plain",
        workspace_id=test_workspace.id,
        uploader_id=other.id,
        status="success",
    )
    session.add(file_obj)
    session.flush()
    context_token = _context(test_user.id, test_workspace.id, "viewer")
    try:
        from unittest.mock import patch

        with patch("app.mcp.server.SessionLocal", return_value=session):
            result = json.loads(asyncio.run(get_file_info(file_obj.id)))
        assert result["id"] == file_obj.id
    finally:
        reset_mcp_context(context_token)
