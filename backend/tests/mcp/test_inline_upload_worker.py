"""内联适配在事件循环外运行, 先于独立 worker 持有 SQL 单元."""
import asyncio
import json
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from app.features.file.upload_service import FileUploadService
from app.mcp import server
from app.mcp.context import McpRequestContext, reset_mcp_context, set_mcp_context


async def test_blocked_adaptation_leaves_loop_free_and_keeps_sql_session_on_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop_thread = threading.get_ident()
    entered, release = threading.Event(), threading.Event()

    class Session:
        owner: int

        def __init__(self) -> None:
            self.owner = threading.get_ident()
            assert self.owner != loop_thread
            sessions.append(self)

        def close(self) -> None:
            assert threading.get_ident() == self.owner
            closed.append(self)

    sessions: list[Session] = []
    closed: list[Session] = []
    original = server._inline_upload_adapter  # pyright: ignore[reportPrivateUsage]

    def adapt(*args: Any, **kwargs: Any) -> Any:
        assert threading.get_ident() != loop_thread
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    def authorize(session: Session, context: McpRequestContext) -> None:
        assert threading.get_ident() == session.owner
        assert context.workspace_id == 2 and context.user_id == 1

    def upload(
        _service: Any,
        session: Session,
        workspace: int,
        user: int,
        adapter: Any,
        parent: Any,
    ) -> Any:
        assert threading.get_ident() == session.owner
        assert adapter.filename == "note.txt" and workspace == 2 and user == 1
        return SimpleNamespace(to_dict=lambda: {"id": 7})

    monkeypatch.setattr(server, "_inline_upload_adapter", adapt)
    monkeypatch.setattr(server, "SessionLocal", Session)
    monkeypatch.setattr(server, "assert_current_write_permission", authorize)
    monkeypatch.setattr(FileUploadService, "create_uploaded_file", upload)
    token = set_mcp_context(McpRequestContext(1, 2, "editor"))
    task = asyncio.create_task(server.upload_file("note.txt", "hello"))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        assert not task.done() and not sessions

        async def probe() -> bool:
            assert not release.is_set()
            return True

        assert await asyncio.wait_for(probe(), 1)
    finally:
        release.set()
        try:
            raw_result = await task
            result: dict[str, Any] = json.loads(raw_result)
        finally:
            reset_mcp_context(token)
    assert result["success"] and result["file"]["id"] == 7
    assert len(sessions) == 1 and closed == sessions


async def test_viewer_is_rejected_before_any_inline_adaptation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapt = MagicMock(side_effect=AssertionError("read-only input must not be adapted"))
    monkeypatch.setattr(server, "_inline_upload_adapter", adapt)
    token = set_mcp_context(McpRequestContext(1, 2, "viewer"))
    try:
        with pytest.raises(PermissionError, match="read-only"):
            _ = await server.upload_file("note.txt", "hello")
    finally:
        reset_mcp_context(token)
    adapt.assert_not_called()


async def test_upload_factory_failure_returns_safe_tool_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail() -> None:
        raise RuntimeError("private-upload-database-details")

    monkeypatch.setattr(server, "SessionLocal", fail)
    token = set_mcp_context(McpRequestContext(1, 2, "editor"))
    try:
        result: dict[str, Any] = json.loads(await server.upload_file("note.txt", "hello"))
    finally:
        reset_mcp_context(token)
    assert result == {"error": "Service temporarily unavailable"}
