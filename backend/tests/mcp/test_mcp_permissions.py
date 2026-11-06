"""MCP 角色与跨上传者鉴权回归测试."""

import asyncio
import contextvars
import json
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from app.mcp.context import McpRequestContext, reset_mcp_context, set_mcp_context
from app.mcp.server import create_folder, get_file_info, move_file
from app.models.file import File
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember


def _context(
    user_id: int, workspace_id: int, role: str
) -> contextvars.Token[McpRequestContext | None]:
    return set_mcp_context(
        McpRequestContext(user_id=user_id, workspace_id=workspace_id, role=role)
    )


def test_viewer_can_read_but_cannot_create_folder(
    session: Session, test_user: User, test_workspace: Workspace
) -> None:
    member = (
        session.query(WorkspaceMember)
        .filter_by(workspace_id=int(test_workspace.id), user_id=int(test_user.id))
        .one()
    )
    member.role = "viewer"
    session.flush()
    context_token = _context(int(test_user.id), int(test_workspace.id), "viewer")
    try:
        with pytest.raises(PermissionError):
            asyncio.run(create_folder("forbidden"))
    finally:
        reset_mcp_context(context_token)


def test_editor_can_update_file_uploaded_by_another_member(
    session: Session, test_user: User, test_workspace: Workspace
) -> None:
    other = User(username="other-uploader", role="common")
    other.set_password("password")
    session.add(other)
    session.flush()
    session.add(
        WorkspaceMember(
            workspace_id=int(test_workspace.id),
            user_id=int(other.id),
            role="viewer",
        )
    )
    file_obj = File(
        name="shared.txt",
        file_path="shared.txt",
        file_size=10,
        mime_type="text/plain",
        workspace_id=int(test_workspace.id),
        uploader_id=int(other.id),
        status="success",
    )
    session.add(file_obj)
    session.flush()

    context_token = _context(int(test_user.id), int(test_workspace.id), "editor")
    try:
        with patch("app.mcp.server.SessionLocal", return_value=session):
            raw_result = asyncio.run(
                move_file(int(file_obj.id), new_name="renamed-by-editor.txt")
            )
            result: dict[str, Any] = json.loads(raw_result)
        assert result["name"] == "renamed-by-editor.txt"
    finally:
        reset_mcp_context(context_token)


def test_member_can_read_file_from_workspace_even_when_not_uploader(
    session: Session, test_user: User, test_workspace: Workspace
) -> None:
    other = User(username="reader-uploader", role="common")
    other.set_password("password")
    session.add(other)
    session.flush()
    session.add(
        WorkspaceMember(
            workspace_id=int(test_workspace.id),
            user_id=int(other.id),
            role="viewer",
        )
    )
    file_obj = File(
        name="member-file.txt",
        file_path="member-file.txt",
        file_size=10,
        mime_type="text/plain",
        workspace_id=int(test_workspace.id),
        uploader_id=int(other.id),
        status="success",
    )
    session.add(file_obj)
    session.flush()
    context_token = _context(int(test_user.id), int(test_workspace.id), "viewer")
    try:
        with patch("app.mcp.server.SessionLocal", return_value=session):
            raw_result = asyncio.run(get_file_info(int(file_obj.id)))
            result: dict[str, Any] = json.loads(raw_result)
        assert result["id"] == file_obj.id
    finally:
        reset_mcp_context(context_token)
