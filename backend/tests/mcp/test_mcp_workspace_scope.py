"""MCP workspace scoping regression tests."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

from app.mcp.context import McpRequestContext, set_mcp_context
from app.mcp.server import list_files, search_files


def _set_context(user_id: int, workspace_id: int, role: str = "admin"):
    return set_mcp_context(
        McpRequestContext(user_id=user_id, workspace_id=workspace_id, role=role)
    )


def test_search_passes_workspace_id_not_user_id(session, test_user, test_workspace):
    context_token = _set_context(test_user.id, test_workspace.id)
    try:
        result = {"items": [], "total": 0}
        with patch("app.mcp.server.SessionLocal", return_value=session), patch(
            "app.mcp.server.file_service.search_files",
            new_callable=AsyncMock,
            return_value=result,
        ) as search:
            payload = json.loads(asyncio.run(search_files("report")))

        assert payload == result
        assert search.await_args.args[1] == test_workspace.id
        assert search.await_args.args[1] == test_workspace.id
    finally:
        from app.mcp.context import reset_mcp_context

        reset_mcp_context(context_token)


def test_list_files_resolves_root_in_selected_workspace(session, test_user, test_workspace):
    context_token = _set_context(test_user.id, test_workspace.id)
    try:
        with patch(
            "app.mcp.server.folder_service.get_root_folder_id", return_value=77
        ) as root_id, patch(
            "app.mcp.server.file_service.get_files_and_folders",
            return_value={"files": [], "folders": []},
        ) as list_items, patch("app.mcp.server.SessionLocal", return_value=session):
            asyncio.run(list_files())

        assert root_id.call_args.args[1] == test_workspace.id
        assert list_items.call_args.args[1] == test_workspace.id
    finally:
        from app.mcp.context import reset_mcp_context

        reset_mcp_context(context_token)
