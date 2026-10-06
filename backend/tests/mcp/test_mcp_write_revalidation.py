"""A captured administrator context must not authorize a later database write."""
import asyncio
import json
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session

from app.mcp import server
from app.mcp.context import McpRequestContext, set_mcp_context, reset_mcp_context
from app.models.file import File
from app.models.folder import Folder
from app.models.workspace import WorkspaceMember


@pytest.mark.parametrize("change", ["viewer", "removed"])
@pytest.mark.parametrize("operation", ["create_folder", "move_file", "delete_file", "move_folder",
                                       "delete_folder", "batch_delete", "upload_file"])
def test_captured_admin_role_cannot_reach_mutation_after_change(
        session, test_workspace, test_user, test_folder, monkeypatch, change, operation):
    file = File(name="original",file_path="original",workspace_id=test_workspace.id,uploader_id=test_user.id)
    session.add(file)
    session.flush()
    member = session.query(WorkspaceMember).filter_by(workspace_id=test_workspace.id,user_id=test_user.id).one()
    if change == "viewer":
        member.role = "viewer"
    else:
        session.delete(member)
    session.flush()
    monkeypatch.setattr(server,"SessionLocal",lambda: Session(
        bind=session.get_bind(),join_transaction_mode="create_savepoint"))
    mutations = []
    for module,name in ((server.folder_service,"create_folder"),(server.folder_service,"update_folder"),
                         (server.folder_service,"delete_folder"),(server.file_service,"update_file"),
                         (server.file_service,"delete_file"),(server.file_service,"create_uploaded_file")):
        forbidden = MagicMock(side_effect=AssertionError("mutation reached after permission change"))
        monkeypatch.setattr(module,name,forbidden)
        mutations.append(forbidden)
    kwargs = {
        "create_folder":{"name":"forbidden"},
        "move_file":{"file_id":file.id,"new_name":"forbidden"},
        "delete_file":{"file_id":file.id},
        "move_folder":{"folder_id":test_folder.id,"new_name":"forbidden"},
        "delete_folder":{"folder_id":test_folder.id},
        "batch_delete":{"items":[server.BatchDeleteItem(id=file.id)]},
        "upload_file":{"filename":"forbidden.txt","content":"private body"},
    }[operation]
    token = set_mcp_context(McpRequestContext(user_id=test_user.id,workspace_id=test_workspace.id,role="admin"))
    try:
        payload = json.loads(asyncio.run(getattr(server,operation)(**kwargs)))
        expected = "需要编辑者或管理员权限" if change == "viewer" else "您不是该工作空间的成员"
        if operation == "batch_delete":
            assert payload["deleted_ids"] == [] and payload["deleted_count"] == 0
            assert payload["errors"] == [{"id":file.id,"error":expected}]
        else:
            assert payload == {"error":expected}
        assert all(not mutation.called for mutation in mutations)
        assert session.get(File,file.id).name == "original"
        assert session.get(Folder,test_folder.id).name == test_folder.name
    finally:
        reset_mcp_context(token)
