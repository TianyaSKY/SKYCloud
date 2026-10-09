"""Revalidation must precede cache-backed protocol reads."""
import asyncio
import json
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session

from app.mcp import server
from app.mcp.context import McpRequestContext, set_mcp_context, reset_mcp_context
from app.models.file import File
from app.models.folder import Folder
from app.models.workspace import Workspace, WorkspaceMember


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


def test_root_file_helper_excludes_foreign_file_with_local_parent(session,test_workspace,test_user):
    from app.features.folder.service import get_files_in_root_folder
    root = session.query(Folder).filter_by(workspace_id=test_workspace.id,parent_id=None).one()
    foreign = Workspace(name="foreign",owner_id=test_user.id)
    session.add(foreign)
    session.flush()
    session.add_all([
        File(name="local",file_path="local",workspace_id=test_workspace.id,uploader_id=test_user.id,parent_id=root.id),
        File(name="private",file_path="private",workspace_id=foreign.id,uploader_id=test_user.id,parent_id=root.id),
    ])
    session.flush()
    assert [row["name"] for row in get_files_in_root_folder(session,test_workspace.id)] == ["local"]


def test_root_file_cache_ignores_legacy_unscoped_payload(monkeypatch):
    import fakeredis
    from sqlalchemy import create_engine
    from app.infra.extensions import Base
    from app.models.user import User
    from app.infra import cache
    from app.features.folder import service
    store = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(cache, "redis_client", store)
    # Use real commits: the shared fixture's outer rollback transaction keeps
    # writes uncommitted even after its Session commits a savepoint.
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            user = User(username='scoped-cache', password_hash='unused')
            session.add(user); session.flush()
            workspace = Workspace(name='scoped-cache', owner_id=user.id)
            session.add(workspace); session.flush()
            root = Folder(name='/',workspace_id=workspace.id)
            session.add(root); session.flush()
            session.add(File(name='local',file_path='local',workspace_id=workspace.id,
                             uploader_id=user.id,parent_id=root.id))
            session.commit()
            workspace_id = workspace.id
            store.set(f'workspace:root_files:{workspace_id}',
                      json.dumps([{'name':'PRIVATE_LEGACY_FOREIGN_FILE'}]),ex=3600)
            rows = service.get_files_in_root_folder(session,workspace_id)
            assert [row['name'] for row in rows] == ['local']
            monkeypatch.setattr(session,'query',MagicMock(side_effect=AssertionError('unexpected SQL on warm cache')))
            assert service.get_files_in_root_folder(session,workspace_id) == rows
    finally:
        engine.dispose()
