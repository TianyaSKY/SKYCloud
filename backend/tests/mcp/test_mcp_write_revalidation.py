"""捕获的管理员上下文绝不能授权后续数据库写入测试."""

import asyncio
from collections.abc import Callable, Coroutine
import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session

from app.features.file.upload_service import FileUploadService
from app.mcp import server
from app.mcp.context import McpRequestContext, reset_mcp_context, set_mcp_context
from app.models.file import File
from app.models.folder import Folder
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember


@pytest.mark.parametrize("change", ["viewer", "removed"])
@pytest.mark.parametrize(
    "operation",
    [
        "create_folder",
        "move_file",
        "delete_file",
        "move_folder",
        "delete_folder",
        "batch_delete",
        "upload_file",
    ],
)
def test_captured_admin_role_cannot_reach_mutation_after_change(
    session: Session,
    test_workspace: Workspace,
    test_user: User,
    test_folder: Folder,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
    operation: str,
) -> None:
    file_obj = File(
        name="original",
        file_path="original",
        workspace_id=int(test_workspace.id),
        uploader_id=int(test_user.id),
    )
    session.add(file_obj)
    session.flush()
    member = (
        session.query(WorkspaceMember)
        .filter_by(workspace_id=int(test_workspace.id), user_id=int(test_user.id))
        .one()
    )
    if change == "viewer":
        member.role = "viewer"
    else:
        session.delete(member)
    session.flush()
    monkeypatch.setattr(
        server,
        "SessionLocal",
        lambda: Session(
            bind=session.get_bind(), join_transaction_mode="create_savepoint"
        ),
    )
    mutations: list[MagicMock] = []
    for module, name in (
        (server.folder_service, "create_folder"),
        (server.folder_service, "update_folder"),
        (server.folder_service, "delete_folder"),
        (server.file_service, "update_file"),
        (server.file_service, "delete_file"),
        (FileUploadService, "create_uploaded_file"),
    ):
        forbidden = MagicMock(
            side_effect=AssertionError("mutation reached after permission change")
        )
        monkeypatch.setattr(module, name, forbidden)
        mutations.append(forbidden)

    kwargs_map: dict[str, dict[str, Any]] = {
        "create_folder": {"name": "forbidden"},
        "move_file": {"file_id": int(file_obj.id), "new_name": "forbidden"},
        "delete_file": {"file_id": int(file_obj.id)},
        "move_folder": {
            "folder_id": int(test_folder.id),
            "new_name": "forbidden",
        },
        "delete_folder": {"folder_id": int(test_folder.id)},
        "batch_delete": {
            "items": [server.BatchDeleteItem(id=int(file_obj.id))]
        },
        "upload_file": {
            "filename": "forbidden.txt",
            "content": "private body",
        },
    }
    kwargs = kwargs_map[operation]
    token = set_mcp_context(
        McpRequestContext(
            user_id=int(test_user.id),
            workspace_id=int(test_workspace.id),
            role="admin",
        )
    )
    try:
        fn: Callable[..., Coroutine[Any, Any, str]] = getattr(server, operation)
        payload: dict[str, Any] = json.loads(asyncio.run(fn(**kwargs)))
        expected = (
            "需要编辑者或管理员权限"
            if change == "viewer"
            else "您不是该工作空间的成员"
        )
        if operation == "batch_delete":
            assert (
                payload["deleted_ids"] == [] and payload["deleted_count"] == 0
            )
            assert payload["errors"] == [
                {"id": file_obj.id, "error": expected}
            ]
        else:
            assert payload == {"error": expected}
        assert all(not mutation.called for mutation in mutations)
        file_rec = session.get(File, file_obj.id)
        assert file_rec is not None and file_rec.name == "original"
        folder_rec = session.get(Folder, test_folder.id)
        assert folder_rec is not None and folder_rec.name == test_folder.name
    finally:
        reset_mcp_context(token)
