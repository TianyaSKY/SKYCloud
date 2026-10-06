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
