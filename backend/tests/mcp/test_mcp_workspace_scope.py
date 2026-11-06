"""MCP 工作区范围隔离回归测试."""

import asyncio
import contextvars
import json
from typing import Any
from unittest.mock import AsyncMock, patch

from sqlalchemy.orm import Session

from app.mcp.context import (
    McpRequestContext,
    reset_mcp_context,
    set_mcp_context,
)
from app.mcp.server import list_files, search_files
from app.models.user import User
from app.models.workspace import Workspace


def _set_context(
    user_id: int, workspace_id: int, role: str = "admin"
) -> contextvars.Token[McpRequestContext | None]:
    return set_mcp_context(
        McpRequestContext(user_id=user_id, workspace_id=workspace_id, role=role)
    )


def test_search_passes_workspace_id_not_user_id(
    session: Session, test_user: User, test_workspace: Workspace
) -> None:
    context_token = _set_context(int(test_user.id), int(test_workspace.id))
    try:
        result: dict[str, Any] = {"items": [], "total": 0}
        with (
            patch("app.mcp.server.SessionLocal", return_value=session),
            patch(
                "app.mcp.server.file_search_service.search_files",
                new_callable=AsyncMock,
                return_value=result,
            ) as search,
        ):
            payload = json.loads(asyncio.run(search_files("report")))

        assert payload == result
        assert search.await_args is not None
        assert search.await_args.args[1] == test_workspace.id
    finally:
        reset_mcp_context(context_token)


def test_list_files_resolves_root_in_selected_workspace(
    session: Session, test_user: User, test_workspace: Workspace
) -> None:
    context_token = _set_context(int(test_user.id), int(test_workspace.id))
    try:
        with (
            patch(
                "app.mcp.server.folder_service.get_root_folder_id",
                return_value=77,
            ) as root_id,
            patch(
                "app.mcp.server.file_service.get_files_and_folders",
                return_value={"files": [], "folders": []},
            ) as list_items,
            patch("app.mcp.server.SessionLocal", return_value=session),
        ):
            asyncio.run(list_files())

        assert root_id.call_args is not None
        assert root_id.call_args.args[1] == test_workspace.id
        assert list_items.call_args is not None
        assert list_items.call_args.args[1].workspace_id == test_workspace.id
    finally:
        reset_mcp_context(context_token)
