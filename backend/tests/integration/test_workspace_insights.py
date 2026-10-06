"""Opt-in real PostgreSQL MCP reads and independently committed revocation."""
import asyncio
import json
import os

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.mcp import server
from app.mcp.context import McpRequestContext, set_mcp_context, reset_mcp_context
from entry.migrate import upgrade
from tests.integration.test_migrations import postgres

pytestmark = pytest.mark.skipif(os.getenv("RUN_WORKSPACE_INSIGHTS_TESTS") != "1",
                               reason="Opt-in isolated PostgreSQL workspace insights")


@pytest.mark.parametrize("tool", [server.get_storage_overview, server.get_folder_tree])
def test_scoped_read_then_independent_membership_removal(postgres, monkeypatch, tool):
    upgrade(postgres)
    with postgres.begin() as db:
        db.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (1,'reader','x','common'),(2,'foreign','x','common')"))
        db.execute(text("INSERT INTO workspaces(id,name,owner_id) VALUES (1,'one',1),(2,'two',2)"))
        db.execute(text("INSERT INTO workspace_members(workspace_id,user_id,role) VALUES (1,1,'viewer'),(2,2,'admin')"))
        db.execute(text("INSERT INTO folder(id,name,workspace_id,parent_id) VALUES (1,'/',1,NULL),(2,'private',2,1)"))
        db.execute(text("""INSERT INTO files(name,file_path,file_size,status,workspace_id,uploader_id)
            VALUES ('allowed','allowed',1024,'success',1,1),('private','private',99999,'fail',2,2)"""))
    monkeypatch.setattr(server,"SessionLocal",sessionmaker(bind=postgres))
    token = set_mcp_context(McpRequestContext(user_id=1,workspace_id=1,role="viewer"))
    try:
        response = json.loads(asyncio.run(tool()))
        if tool is server.get_storage_overview:
            assert response == {"total_files":1,"total_folders":1,"total_size_bytes":1024,
                "total_size_human":"1.0 KB","status_breakdown":{"success":1,"pending":0,"processing":0,"fail":0}}
        else:
            assert response == [{"id":1,"name":"/"}]
        with postgres.begin() as independent:
            independent.execute(text("DELETE FROM workspace_members WHERE workspace_id=1 AND user_id=1"))
        # Same verified request identity; fresh transaction must reject it now.
        assert json.loads(asyncio.run(tool())) == {"error":"您不是该工作空间的成员"}
    finally:
        reset_mcp_context(token)


@pytest.mark.parametrize("operation", ["create_folder","move_file","delete_file","move_folder",
                                       "delete_folder","batch_delete","upload_file"])
def test_write_tool_rejects_independent_role_downgrade(postgres, monkeypatch, operation):
    upgrade(postgres)
    with postgres.begin() as db:
        db.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (1,'writer','x','common')"))
        db.execute(text("INSERT INTO workspaces(id,name,owner_id) VALUES (1,'one',1)"))
        db.execute(text("INSERT INTO workspace_members(workspace_id,user_id,role) VALUES (1,1,'admin')"))
        db.execute(text("INSERT INTO folder(id,name,workspace_id,parent_id) VALUES (1,'/',1,NULL),(2,'original',1,1)"))
        db.execute(text("INSERT INTO files(id,name,file_path,file_size,status,workspace_id,uploader_id) VALUES (1,'original','original',3,'success',1,1)"))
    monkeypatch.setattr(server,"SessionLocal",sessionmaker(bind=postgres))
    token = set_mcp_context(McpRequestContext(user_id=1,workspace_id=1,role="admin"))
    try:
        with postgres.begin() as independent:
            independent.execute(text("UPDATE workspace_members SET role='viewer' WHERE workspace_id=1 AND user_id=1"))
        arguments = {
            "create_folder":{"name":"forbidden"},"move_file":{"file_id":1,"new_name":"forbidden"},
            "delete_file":{"file_id":1},"move_folder":{"folder_id":2,"new_name":"forbidden"},
            "delete_folder":{"folder_id":2},"batch_delete":{"items":[server.BatchDeleteItem(id=1)]},
            "upload_file":{"filename":"forbidden.txt","content":"abc"},
        }[operation]
        result = json.loads(asyncio.run(getattr(server,operation)(**arguments)))
        error = "需要编辑者或管理员权限"
        if operation == "batch_delete":
            assert result == {"deleted_count":0,"deleted_ids":[],"errors":[{"id":1,"error":error}]}
        else:
            assert result == {"error":error}
        with postgres.connect() as db:
            assert db.execute(text("SELECT id,name FROM files")).all() == [(1,'original')]
            assert db.execute(text("SELECT id,name FROM folder ORDER BY id")).all() == [(1,'/'),(2,'original')]
            assert db.scalar(text("SELECT count(*) FROM upload_reservations")) == 0
    finally:
        reset_mcp_context(token)


def test_batch_delete_revalidates_after_each_committed_item(postgres, monkeypatch):
    upgrade(postgres)
    with postgres.begin() as db:
        db.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (1,'writer','x','common')"))
        db.execute(text("INSERT INTO workspaces(id,name,owner_id) VALUES (1,'one',1)"))
        db.execute(text("INSERT INTO workspace_members(workspace_id,user_id,role) VALUES (1,1,'admin')"))
        db.execute(text("INSERT INTO files(id,name,file_path,file_size,status,workspace_id,uploader_id) VALUES (1,'first','first',3,'success',1,1),(2,'second','second',3,'success',1,1)"))
    monkeypatch.setattr(server,"SessionLocal",sessionmaker(bind=postgres))
    def delete_and_downgrade(session,file_id):
        assert file_id == 1, 'second mutation must not be reached'
        session.execute(text("DELETE FROM files WHERE id=:id"),{"id":file_id})
        session.commit()
        with postgres.begin() as independent:
            independent.execute(text("UPDATE workspace_members SET role='viewer' WHERE workspace_id=1 AND user_id=1"))
    monkeypatch.setattr(server.file_service,"delete_file",delete_and_downgrade)
    token = set_mcp_context(McpRequestContext(user_id=1,workspace_id=1,role="admin"))
    try:
        result = json.loads(asyncio.run(server.batch_delete([
            server.BatchDeleteItem(id=1),server.BatchDeleteItem(id=2)])))
        assert result == {"deleted_count":1,"deleted_ids":[1],
                          "errors":[{"id":2,"error":"需要编辑者或管理员权限"}]}
        with postgres.connect() as db:
            assert db.execute(text("SELECT id FROM files")).scalars().all() == [2]
    finally:
        reset_mcp_context(token)
