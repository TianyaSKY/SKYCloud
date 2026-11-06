"""真实 SQL 范围、读取模型与 MCP worker 所有权测试."""

import asyncio
from collections.abc import Callable, Coroutine
import json
import threading
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.features.workspace import insights
from app.mcp import server
from app.mcp.context import McpRequestContext, reset_mcp_context, set_mcp_context
from app.models.file import File
from app.models.folder import Folder
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember


def test_insights_scope_aggregates_and_depth(
    session: Session, test_workspace: Workspace, test_user: User
) -> None:
    foreign = Workspace(name="foreign", owner_id=int(test_user.id))
    session.add(foreign)
    session.flush()
    root = (
        session.query(Folder)
        .filter_by(workspace_id=int(test_workspace.id), parent_id=None)
        .one()
    )
    child = Folder(
        name="child", workspace_id=int(test_workspace.id), parent_id=int(root.id)
    )
    session.add(child)
    session.flush()
    session.add_all(
        [
            Folder(
                name="grandchild",
                workspace_id=int(test_workspace.id),
                parent_id=int(child.id),
            ),
            Folder(
                name="private",
                workspace_id=int(foreign.id),
                parent_id=int(root.id),
            ),
            File(
                name="one",
                file_path="one",
                file_size=1024,
                status="success",
                workspace_id=int(test_workspace.id),
                uploader_id=int(test_user.id),
            ),
            File(
                name="two",
                file_path="two",
                file_size=512,
                status="fail",
                workspace_id=int(test_workspace.id),
                uploader_id=int(test_user.id),
            ),
            File(
                name="foreign",
                file_path="foreign",
                file_size=99999,
                status="success",
                workspace_id=int(foreign.id),
                uploader_id=int(test_user.id),
            ),
        ]
    )
    session.flush()
    assert insights.storage_overview(
        session, int(test_workspace.id), int(test_user.id)
    ) == {
        "total_files": 2,
        "total_folders": 3,
        "total_size_bytes": 1536,
        "total_size_human": "1.5 KB",
        "status_breakdown": {
            "success": 1,
            "pending": 0,
            "processing": 0,
            "fail": 1,
        },
    }
    assert insights.folder_tree(
        session, int(test_workspace.id), int(test_user.id), 0
    ) == [{"id": root.id, "name": "/"}]
    depth_two = insights.folder_tree(
        session, int(test_workspace.id), int(test_user.id), 2
    )
    assert depth_two == [
        {"id": root.id, "name": "/", "children": [{"id": child.id, "name": "child"}]}
    ]
    deep = insights.folder_tree(
        session, int(test_workspace.id), int(test_user.id), 999
    )
    assert "grandchild" in json.dumps(deep) and "private" not in json.dumps(deep)


@pytest.mark.parametrize(
    "tool", [server.get_storage_overview, server.get_folder_tree]
)
def test_verified_context_cannot_read_after_membership_revocation(
    session: Session,
    test_workspace: Workspace,
    test_user: User,
    monkeypatch: pytest.MonkeyPatch,
    tool: Callable[[], Coroutine[Any, Any, str]],
) -> None:
    context_token = set_mcp_context(
        McpRequestContext(
            user_id=int(test_user.id),
            workspace_id=int(test_workspace.id),
            role="admin",
        )
    )
    member = (
        session.query(WorkspaceMember)
        .filter_by(
            workspace_id=int(test_workspace.id), user_id=int(test_user.id)
        )
        .one()
    )
    session.delete(member)
    session.flush()
    monkeypatch.setattr(
        server,
        "SessionLocal",
        lambda: Session(
            bind=session.get_bind(), join_transaction_mode="create_savepoint"
        ),
    )
    try:
        payload = json.loads(asyncio.run(tool()))
        assert payload == {"error": "您不是该工作空间的成员"}
    finally:
        reset_mcp_context(context_token)


def test_mcp_queries_and_session_cleanup_stay_on_same_worker(
    session: Session,
    test_workspace: Workspace,
    test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle: list[tuple[str, int]] = []
    query_threads: list[int] = []
    caller_thread = threading.get_ident()

    class OwnedSession(Session):
        def __init__(self) -> None:
            lifecycle.append(("create", threading.get_ident()))
            super().__init__(
                bind=session.get_bind(),
                join_transaction_mode="create_savepoint",
            )

        def close(self) -> None:
            lifecycle.append(("close", threading.get_ident()))
            super().close()

    monkeypatch.setattr(server, "SessionLocal", OwnedSession)

    def query_thread(*args: Any) -> None:
        query_threads.append(threading.get_ident())

    connection = session.get_bind()
    assert connection is not None
    event.listen(connection, "before_cursor_execute", query_thread)
    token = set_mcp_context(
        McpRequestContext(
            user_id=int(test_user.id),
            workspace_id=int(test_workspace.id),
            role="viewer",
        )
    )
    try:

        async def read() -> None:
            overview: dict[str, Any] = json.loads(
                await server.get_storage_overview()
            )
            tree: list[dict[str, Any]] = json.loads(
                await server.get_folder_tree()
            )
            assert overview["total_files"] == 0
            assert overview["total_size_bytes"] == 0
            assert tree[0]["name"] == "/"

        asyncio.run(read())
        assert [action for action, _ in lifecycle] == [
            "create",
            "close",
            "create",
            "close",
        ]
        worker_threads: set[int] = {thread for _, thread in lifecycle}
        assert caller_thread not in worker_threads
        assert query_threads and set(query_threads) <= worker_threads
    finally:
        reset_mcp_context(token)
        event.remove(connection, "before_cursor_execute", query_thread)
