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
from tests.integration.test_runtime_security import infrastructure

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


@pytest.mark.parametrize("operation", ["list_files","get_user_folders","get_current_user"])
def test_remaining_reads_revalidate_before_warm_cache(postgres,monkeypatch,operation):
    upgrade(postgres)
    with postgres.begin() as db:
        db.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (1,'reader','x','common')"))
        db.execute(text("INSERT INTO workspaces(id,name,owner_id) VALUES (1,'one',1)"))
        db.execute(text("INSERT INTO workspace_members(workspace_id,user_id,role) VALUES (1,1,'admin')"))
        db.execute(text("INSERT INTO folder(id,name,workspace_id,parent_id) VALUES (1,'/',1,NULL),(2,'private-name',1,1)"))
        db.execute(text("INSERT INTO files(id,name,file_path,file_size,status,workspace_id,uploader_id,parent_id) VALUES (1,'private-file','private',3,'success',1,1,1)"))
    monkeypatch.setattr(server,"SessionLocal",sessionmaker(bind=postgres))
    token = set_mcp_context(McpRequestContext(user_id=1,workspace_id=1,role="admin"))
    try:
        warm = json.loads(asyncio.run(getattr(server,operation)()))
        if operation == "list_files":
            assert warm["files"]["items"][0]["name"] == "private-file"
        elif operation == "get_user_folders":
            assert any(folder["name"] == "private-name" for folder in warm)
        else:
            assert warm["workspace_role"] == "admin"
            with postgres.begin() as independent:
                independent.execute(text("UPDATE workspace_members SET role='viewer' WHERE workspace_id=1 AND user_id=1"))
            assert json.loads(asyncio.run(server.get_current_user()))["workspace_role"] == "viewer"
        from unittest.mock import MagicMock
        cached = MagicMock(return_value=warm)
        if operation == "list_files":
            monkeypatch.setattr(server.folder_service,"get_root_folder_id",cached)
        elif operation == "get_user_folders":
            monkeypatch.setattr(server.folder_service,"get_folders",cached)
        with postgres.begin() as independent:
            independent.execute(text("DELETE FROM workspace_members WHERE workspace_id=1 AND user_id=1"))
        assert json.loads(asyncio.run(getattr(server,operation)())) == {"error":"您不是该工作空间的成员"}
        cached.assert_not_called()
    finally:
        reset_mcp_context(token)


@pytest.mark.parametrize('mode', ['fuzzy', 'vector'])
async def test_search_scope_and_committed_revocation_before_cached_or_vector_reads(postgres, infrastructure, monkeypatch, mode):
    from unittest.mock import AsyncMock
    from app.exceptions import PermissionDeniedError
    from app.features.file import search_service
    from app.infra import cache
    upgrade(postgres)
    near = [1.0] + [0.0]*1023
    far = [0.0, 1.0] + [0.0]*1022
    with postgres.begin() as db:
        db.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (1,'search-reader','x','common'),(2,'search-other','x','common')"))
        db.execute(text("INSERT INTO workspaces(id,name,owner_id) VALUES (1,'one',1),(2,'other',2)"))
        db.execute(text("INSERT INTO workspace_members(workspace_id,user_id,role) VALUES (1,1,'viewer'),(2,2,'admin')"))
        db.execute(text("INSERT INTO files(id,name,file_path,file_size,status,workspace_id,uploader_id,vector_info) VALUES (1,'target','one',1,'success',1,1,CAST(:near AS vector)),(2,'target','foreign',1,'success',2,2,CAST(:near AS vector)),(3,'other','far',1,'success',1,1,CAST(:far AS vector))"), {'near':json.dumps(near),'far':json.dumps(far)})
    monkeypatch.setattr(server, 'SessionLocal', sessionmaker(bind=postgres))
    monkeypatch.setattr(cache, 'redis_client', infrastructure['redis'])
    monkeypatch.setattr(search_service, 'embedding_desc', AsyncMock(return_value=near))
    monkeypatch.setattr(search_service, 'get_embedding_model_config', lambda: {})
    token = set_mcp_context(McpRequestContext(1,1,'viewer'))
    try:
        first = json.loads(await server.search_files('target', search_type=mode))
        expected = [1] if mode == 'fuzzy' else [1,3]
        assert [item['id'] for item in first['items']] == expected
        assert first['total'] == len(expected)
        assert all(item['workspace_id'] == 1 for item in first['items'])
        if mode == 'fuzzy':
            assert infrastructure['redis'].exists('search:fuzzy:1:target:1:10:fuzzy') == 1
        with postgres.begin() as remover:
            remover.execute(text('DELETE FROM workspace_members WHERE workspace_id=1 AND user_id=1'))
        with pytest.raises(PermissionDeniedError):
            await server.search_files('target', search_type=mode)
    finally:
        reset_mcp_context(token)


async def test_worker_cleanup_invalidation_preserves_commit_and_rolls_back_uncommitted_work(postgres, monkeypatch):
    """Explicit close fault seam; real PG transactions and SQLAlchemy invalidation."""
    from sqlalchemy.orm import Session
    from sqlalchemy.exc import StatementError
    from loguru import logger
    upgrade(postgres)
    private = 'private-cleanup-sql-sentinel'
    class CloseFaultSession(Session):
        def close(self):
            raise StatementError(private, 'SELECT :password', {'password': private}, ValueError(private))
    monkeypatch.setattr(server, 'SessionLocal', sessionmaker(bind=postgres, class_=CloseFaultSession))
    records = []
    sink = logger.add(lambda message: records.append(message.record))
    original = RuntimeError('original business failure')
    def committed(db):
        db.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (1,'committed','x','common')"))
        db.commit()
        return {'id': 1}
    def uncommitted(db):
        db.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (2,'uncommitted','x','common')"))
        raise original
    try:
        assert await server._run_sync(committed) == {'id': 1}
        assert postgres.pool.checkedout() == 0
        with pytest.raises(RuntimeError) as caught:
            await server._run_sync(uncommitted)
        assert caught.value is original
        assert postgres.pool.checkedout() == 0
        with sessionmaker(bind=postgres)() as healthy:
            assert healthy.execute(text('SELECT id FROM users ORDER BY id')).scalars().all() == [1]
        assert postgres.pool.checkedout() == 0
    finally:
        logger.remove(sink)
    assert len(records) == 2
    assert all(private not in record['message'] and record['exception'] is None for record in records)


@pytest.mark.parametrize('mode', ['fuzzy','semantic','status'])
async def test_http_worker_reads_revalidate_committed_removal(postgres, infrastructure, monkeypatch, mode):
    """Real SQL/cache behind explicit stale HTTP identity dependency snapshots."""
    import httpx
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from fastapi import FastAPI
    from app.api.dependencies import get_current_user, get_current_workspace
    from app.exceptions import register_exception_handlers
    from app.features.file import router, search_service
    from app.infra.extensions import get_session_factory
    from app.infra import cache
    upgrade(postgres)
    vector = [1.0] + [0.0]*1023
    with postgres.begin() as sql:
        sql.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (1,'reader','x','common')"))
        sql.execute(text("INSERT INTO workspaces(id,name,owner_id) VALUES (1,'one',1)"))
        sql.execute(text("INSERT INTO workspace_members(workspace_id,user_id,role) VALUES (1,1,'viewer')"))
        sql.execute(text("INSERT INTO files(id,name,file_path,file_size,status,workspace_id,uploader_id,vector_info) VALUES (1,'target','one',1,'success',1,1,CAST(:vector AS vector))"),{'vector':json.dumps(vector)})
    monkeypatch.setattr(cache,'redis_client',infrastructure['redis'])
    monkeypatch.setattr(search_service,'embedding_desc',AsyncMock(return_value=vector))
    app = FastAPI(); register_exception_handlers(app); app.include_router(router.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[get_current_workspace] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[get_session_factory] = lambda: sessionmaker(bind=postgres)
    path = '/files/process_status' if mode == 'status' else '/files/search'
    params = {} if mode == 'status' else {'q':'target','type':mode}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test') as client:
        first = await client.get(path,params=params)
        assert first.status_code == 200, first.text
        if mode == 'status':
            assert first.json()['data'] == {'处理中':0,'成功':1,'失败':0}
        else:
            assert [row['id'] for row in first.json()['data']['items']] == [1]
        if mode == 'fuzzy':
            assert infrastructure['redis'].exists('search:fuzzy:1:target:1:10:fuzzy') == 1
        with postgres.begin() as sql:
            sql.execute(text('DELETE FROM workspace_members WHERE workspace_id=1 AND user_id=1'))
        denied = await client.get(path,params=params)
        assert denied.status_code == 403, denied.text
        assert denied.json()['success'] is False


async def test_http_auth_fresh_role_overrides_cached_admin_and_revokes_version(postgres,infrastructure,monkeypatch):
    import httpx
    from fastapi import FastAPI,Depends
    from app.api.dependencies import require_admin
    from app.infra.extensions import get_db,get_session_factory
    from app.features.auth.service import generate_token
    from app.infra import cache
    upgrade(postgres)
    with postgres.begin() as sql:
        sql.execute(text("INSERT INTO users(id,username,password_hash,role,auth_version) VALUES (1,'admin-cache','x','admin',0)"))
    monkeypatch.setattr(cache,'redis_client',infrastructure['redis'])
    factory=sessionmaker(bind=postgres)
    app=FastAPI()
    def database():
        with factory() as db:yield db
    app.dependency_overrides[get_db]=database
    app.dependency_overrides[get_session_factory]=lambda:factory
    @app.get('/admin-only')
    async def admin(user=Depends(require_admin)):
        return {'id':user.id}
    headers={'Authorization':'Bearer '+generate_token(1)}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test') as client:
        assert (await client.get('/admin-only',headers=headers)).status_code==200
        assert infrastructure['redis'].exists('user:profile:1')==1
        with postgres.begin() as sql:
            sql.execute(text("UPDATE users SET role='common' WHERE id=1"))
        assert (await client.get('/admin-only',headers=headers)).status_code==403
        with postgres.begin() as sql:
            sql.execute(text('UPDATE users SET auth_version=1 WHERE id=1'))
        assert (await client.get('/admin-only',headers=headers)).status_code==401


@pytest.mark.parametrize('change',['demote','delete-target'])
async def test_profile_http_current_roles_and_cached_target_lifecycle(postgres,infrastructure,monkeypatch,change):
    import httpx
    from types import SimpleNamespace
    from fastapi import FastAPI
    from app.features.auth import router,user_service
    from app.api.dependencies import get_current_user
    from app.infra.extensions import get_session_factory
    from app.infra import cache
    from app.exceptions import register_exception_handlers
    upgrade(postgres)
    with postgres.begin() as sql:
        sql.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (1,'actor','x','admin'),(2,'target','x','admin')"))
    monkeypatch.setattr(cache,'redis_client',infrastructure['redis'])
    app=FastAPI();register_exception_handlers(app);app.include_router(router.router)
    app.dependency_overrides[get_current_user]=lambda:SimpleNamespace(id=1,role='admin')
    app.dependency_overrides[get_session_factory]=lambda:sessionmaker(bind=postgres)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test') as client:
        first=await client.get('/users/2')
        assert first.status_code==200 and first.json()['data']['role']=='admin'
        assert infrastructure['redis'].exists(user_service.USER_CACHE_PREFIX+':2')==1
        with postgres.begin() as sql:
            sql.execute(text("UPDATE users SET role='common' WHERE id=2"))
        updated=await client.get('/users/2')
        assert updated.status_code==200 and updated.json()['data']['role']=='common'
        with postgres.begin() as sql:
            if change=='demote':sql.execute(text("UPDATE users SET role='common' WHERE id=1"))
            else:sql.execute(text('DELETE FROM users WHERE id=2'))
        denied=await client.get('/users/2')
        assert denied.status_code==(403 if change=='demote' else 404),denied.text


@pytest.mark.parametrize('operation', ['create', 'profile', 'password', 'delete', 'self-password'])
async def test_account_mutations_reject_stale_admin_snapshot(postgres, monkeypatch, operation):
    import httpx
    from types import SimpleNamespace
    from fastapi import FastAPI
    from app.features.auth import router, user_service
    from app.api.dependencies import get_current_user, require_admin
    from app.infra.extensions import get_session_factory
    from app.exceptions import register_exception_handlers
    from app.models.user import User
    upgrade(postgres)
    factory = sessionmaker(bind=postgres)
    with factory() as db:
        actor = User(id=1, username='actor', role='admin')
        target = User(id=2, username='target', role='common')
        actor.set_password('original-password')
        target.set_password('original-password')
        db.add_all([actor, target]); db.commit()
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router.router)
    snapshot = SimpleNamespace(id=1, role='admin')
    app.dependency_overrides[get_current_user] = lambda: snapshot
    app.dependency_overrides[require_admin] = lambda: snapshot
    app.dependency_overrides[get_session_factory] = lambda: factory
    with postgres.begin() as independent:
        independent.execute(text("UPDATE users SET role='common' WHERE id=1"))
    calls = {
        'create': ('POST', '/users', {'username': 'forbidden', 'password': 'new-password'}),
        'profile': ('PUT', '/users/2', {'username': 'forbidden'}),
        'password': ('PUT', '/users/2/password', {'old_password': 'incorrect', 'new_password': 'new-password'}),
        'delete': ('DELETE', '/users/2', None),
        'self-password': ('PUT', '/users/1/password', {'old_password': 'incorrect', 'new_password': 'new-password'}),
    }
    method, path, body = calls[operation]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
        response = await client.request(method, path, json=body)
        assert response.status_code == (400 if operation == 'self-password' else 403), response.text
    with factory() as db:
        assert db.query(User).count() == 2
        assert db.get(User, 1).check_password('original-password')
        assert db.get(User, 2).check_password('original-password')
        assert db.get(User, 1).auth_version == db.get(User, 2).auth_version == 0
        assert db.get(User, 2).username == 'target'


def test_account_authorization_lock_orders_demotion_after_transaction(postgres):
    from sqlalchemy.exc import DBAPIError
    from app.features.auth import user_service
    from app.exceptions import PermissionDeniedError
    upgrade(postgres)
    with postgres.begin() as db:
        db.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (1,'actor','x','admin'),(2,'target','x','common')"))
    factory = sessionmaker(bind=postgres)
    with factory() as owned:
        assert user_service.authorize_account_mutation(owned, 1, 2) == 'admin'
        with postgres.connect() as independent:
            independent.execute(text("SET LOCAL lock_timeout='100ms'"))
            with pytest.raises(DBAPIError) as error:
                independent.execute(text("UPDATE users SET role='common' WHERE id=1"))
            assert error.value.orig.pgcode == '55P03'
            independent.rollback()
        owned.commit()
    with postgres.begin() as independent:
        independent.execute(text("UPDATE users SET role='common' WHERE id=1"))
    with factory() as owned:
        with pytest.raises(PermissionDeniedError):
            user_service.authorize_account_mutation(owned, 1, 2)


async def test_http_workspace_dependencies_detach_and_recheck_membership(postgres):
    import httpx
    from types import SimpleNamespace
    from fastapi import Depends, FastAPI
    from app.api.dependencies import get_current_user, get_current_workspace, require_workspace_role
    from app.infra.extensions import get_session_factory
    from app.exceptions import register_exception_handlers
    upgrade(postgres)
    with postgres.begin() as db:
        db.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (1,'actor','x','common')"))
        db.execute(text("INSERT INTO workspaces(id,name,owner_id) VALUES (1,'one',1)"))
        db.execute(text("INSERT INTO workspace_members(workspace_id,user_id,role) VALUES (1,1,'editor')"))
    app = FastAPI()
    register_exception_handlers(app)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[get_session_factory] = lambda: sessionmaker(bind=postgres)

    @app.get('/context')
    async def context(workspace=Depends(get_current_workspace)):
        assert workspace._sa_instance_state.session is None
        return {'id': workspace.id, 'name': workspace.name}

    @app.get('/editor')
    async def editor(workspace=Depends(require_workspace_role('editor'))):
        assert workspace._sa_instance_state.session is None
        return {'id': workspace.id}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test',
                                 headers={'X-Workspace-Id': '1'}) as client:
        assert (await client.get('/context')).json() == {'id': 1, 'name': 'one'}
        assert (await client.get('/editor')).status_code == 200
        with postgres.begin() as independent:
            independent.execute(text("UPDATE workspace_members SET role='viewer' WHERE workspace_id=1 AND user_id=1"))
        assert (await client.get('/context')).status_code == 200
        assert (await client.get('/editor')).status_code == 403
        with postgres.begin() as independent:
            independent.execute(text('DELETE FROM workspace_members WHERE workspace_id=1 AND user_id=1'))
        assert (await client.get('/context')).status_code == 403
        assert (await client.get('/editor')).status_code == 403
        with postgres.begin() as independent:
            independent.execute(text('DELETE FROM workspaces WHERE id=1'))
        assert (await client.get('/context')).status_code == 404


@pytest.mark.parametrize('operation', ['create', 'update', 'delete', 'organize', 'root', 'all', 'get'])
async def test_folder_http_rechecks_stale_workspace_snapshot(postgres, infrastructure, monkeypatch, operation):
    import httpx
    from types import SimpleNamespace
    from fastapi import FastAPI
    from app.features.folder import router
    from app.api.dependencies import get_current_user
    from app.infra.extensions import get_session_factory
    from app.infra import cache
    from app.exceptions import register_exception_handlers
    upgrade(postgres)
    with postgres.begin() as db:
        db.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (1,'actor','x','common')"))
        db.execute(text("INSERT INTO workspaces(id,name,owner_id) VALUES (1,'one',1)"))
        db.execute(text("INSERT INTO workspace_members(workspace_id,user_id,role) VALUES (1,1,'editor')"))
        db.execute(text("INSERT INTO folder(id,name,workspace_id,parent_id) VALUES (1,'/',1,NULL),(2,'child',1,1)"))
    monkeypatch.setattr(cache, 'redis_client', infrastructure['redis'])
    app = FastAPI(); register_exception_handlers(app); app.include_router(router.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[get_session_factory] = lambda: sessionmaker(bind=postgres)
    for route in router.router.routes:
        for dependency in route.dependant.dependencies:
            if dependency.name == 'workspace':
                app.dependency_overrides[dependency.call] = lambda: SimpleNamespace(id=1)
    calls = {
        'create': ('POST', '/folder', {'name': 'forbidden'}),
        'update': ('PUT', '/folder/2', {'name': 'forbidden'}),
        'delete': ('DELETE', '/folder/2', None),
        'organize': ('POST', '/folder/organize', None),
        'root': ('GET', '/folder/root_id', None),
        'all': ('GET', '/folder/all', None),
        'get': ('GET', '/folder/2', None),
    }
    method, path, body = calls[operation]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
        if method == 'GET':
            assert (await client.get(path)).status_code == 200
            if operation in {'root', 'all'}:
                prefix = 'workspace:root_folder:v2' if operation == 'root' else 'workspace:folders'
                assert infrastructure['redis'].exists(prefix + ':1') == 1
        with postgres.begin() as independent:
            if method == 'GET':
                independent.execute(text('DELETE FROM workspace_members WHERE workspace_id=1 AND user_id=1'))
            else:
                independent.execute(text("UPDATE workspace_members SET role='viewer' WHERE workspace_id=1 AND user_id=1"))
        response = await client.request(method, path, json=body)
        assert response.status_code == 403, response.text
    with postgres.connect() as db:
        assert db.execute(text('SELECT id,name FROM folder ORDER BY id')).all() == [(1, '/'), (2, 'child')]
        assert db.scalar(text('SELECT count(*) FROM organize_tasks')) == 0


def test_folder_write_authorization_lock_blocks_role_change(postgres):
    from sqlalchemy.exc import DBAPIError
    from app.features.workspace.permissions import assert_can_write_locked
    from app.exceptions import PermissionDeniedError
    upgrade(postgres)
    with postgres.begin() as db:
        db.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (1,'actor','x','common')"))
        db.execute(text("INSERT INTO workspaces(id,name,owner_id) VALUES (1,'one',1)"))
        db.execute(text("INSERT INTO workspace_members(workspace_id,user_id,role) VALUES (1,1,'editor')"))
    factory = sessionmaker(bind=postgres)
    with factory() as owned:
        assert_can_write_locked(owned, 1, 1)
        with postgres.connect() as independent:
            independent.execute(text("SET LOCAL lock_timeout='100ms'"))
            with pytest.raises(DBAPIError) as error:
                independent.execute(text("UPDATE workspace_members SET role='viewer' WHERE workspace_id=1 AND user_id=1"))
            assert error.value.orig.pgcode == '55P03'
            independent.rollback()
        owned.commit()
    with postgres.begin() as independent:
        independent.execute(text("UPDATE workspace_members SET role='viewer' WHERE workspace_id=1 AND user_id=1"))
    with factory() as owned:
        with pytest.raises(PermissionDeniedError):
            assert_can_write_locked(owned, 1, 1)


@pytest.mark.parametrize('operation', ['list', 'get', 'download', 'update', 'delete', 'batch', 'retry', 'rebuild'])
async def test_file_http_rejects_stale_membership_before_io(postgres, monkeypatch, operation):
    import httpx
    from types import SimpleNamespace
    from fastapi import FastAPI
    from app.features.file import router
    from app.api.dependencies import get_current_user
    from app.infra.extensions import get_session_factory
    from app.exceptions import register_exception_handlers
    upgrade(postgres)
    with postgres.begin() as db:
        db.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (1,'actor','x','common')"))
        db.execute(text("INSERT INTO workspaces(id,name,owner_id) VALUES (1,'one',1)"))
        db.execute(text("INSERT INTO workspace_members(workspace_id,user_id,role) VALUES (1,1,'editor')"))
        db.execute(text("INSERT INTO files(id,name,file_path,file_size,status,workspace_id,uploader_id) VALUES (1,'original','private',3,'fail',1,1)"))
    app = FastAPI(); register_exception_handlers(app); app.include_router(router.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[get_session_factory] = lambda: sessionmaker(bind=postgres)
    for route in router.router.routes:
        for dependency in route.dependant.dependencies:
            if dependency.name == 'workspace':
                app.dependency_overrides[dependency.call] = lambda: SimpleNamespace(id=1)
    def forbidden_storage():
        raise AssertionError('denied download must not open storage')
    monkeypatch.setattr(router, 'get_storage_client', forbidden_storage)
    calls = {
        'list': ('GET', '/files/list', None), 'get': ('GET', '/files/1', None),
        'download': ('GET', '/files/1/download', None),
        'update': ('PUT', '/files/1', {'name': 'forbidden'}),
        'delete': ('DELETE', '/files/1', None),
        'batch': ('POST', '/files/batch-delete', {'items': [{'id': 1, 'is_folder': False}]}),
        'retry': ('POST', '/files/retry_embedding', {'file_id': 1}),
        'rebuild': ('POST', '/files/rebuild_failed_indexes', None),
    }
    method, path, body = calls[operation]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
        if operation in {'list', 'get'}:
            assert (await client.get(path)).status_code == 200
        with postgres.begin() as independent:
            if method == 'GET':
                independent.execute(text('DELETE FROM workspace_members WHERE workspace_id=1 AND user_id=1'))
            else:
                independent.execute(text("UPDATE workspace_members SET role='viewer' WHERE workspace_id=1 AND user_id=1"))
        response = await client.request(method, path, json=body)
        assert response.status_code == 403, response.text
    with postgres.connect() as db:
        assert db.execute(text('SELECT id,name,status FROM files')).all() == [(1, 'original', 'fail')]
        assert db.scalar(text('SELECT count(*) FROM file_task_outbox')) == 0


async def test_file_batch_delete_rechecks_after_committed_item(postgres, monkeypatch):
    import httpx
    from types import SimpleNamespace
    from fastapi import FastAPI
    from app.features.file import router
    from app.api.dependencies import get_current_user
    from app.infra.extensions import get_session_factory
    from app.exceptions import register_exception_handlers
    upgrade(postgres)
    with postgres.begin() as db:
        db.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (1,'actor','x','common')"))
        db.execute(text("INSERT INTO workspaces(id,name,owner_id) VALUES (1,'one',1)"))
        db.execute(text("INSERT INTO workspace_members(workspace_id,user_id,role) VALUES (1,1,'editor')"))
        db.execute(text("INSERT INTO files(id,name,file_path,file_size,status,workspace_id,uploader_id) VALUES (1,'first','first',3,'success',1,1),(2,'second','second',3,'success',1,1)"))
    def delete_then_demote(session, id):
        assert id == 1, 'second delete must not be dispatched'
        session.execute(text('DELETE FROM files WHERE id=1'))
        session.commit()
        with postgres.begin() as independent:
            independent.execute(text("UPDATE workspace_members SET role='viewer' WHERE workspace_id=1 AND user_id=1"))
    monkeypatch.setattr(router.file_service, 'delete_file', delete_then_demote)
    app = FastAPI(); register_exception_handlers(app); app.include_router(router.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[get_session_factory] = lambda: sessionmaker(bind=postgres)
    for route in router.router.routes:
        for dependency in route.dependant.dependencies:
            if dependency.name == 'workspace':
                app.dependency_overrides[dependency.call] = lambda: SimpleNamespace(id=1)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
        response = await client.post('/files/batch-delete', json={'items': [{'id': 1}, {'id': 2}]})
        assert response.status_code == 403, response.text
    with postgres.connect() as db:
        assert db.execute(text('SELECT id FROM files')).scalars().all() == [2]


@pytest.mark.parametrize('operation', ['create_folder', 'move_file', 'delete_file', 'move_folder',
                                       'delete_folder', 'batch_delete', 'upload_file'])
async def test_mcp_write_authorization_holds_role_lock_at_dispatch(postgres, monkeypatch, operation):
    from sqlalchemy.exc import DBAPIError
    from app.exceptions import BusinessRuleError
    upgrade(postgres)
    with postgres.begin() as db:
        db.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (1,'actor','x','common')"))
        db.execute(text("INSERT INTO workspaces(id,name,owner_id) VALUES (1,'one',1)"))
        db.execute(text("INSERT INTO workspace_members(workspace_id,user_id,role) VALUES (1,1,'editor')"))
        db.execute(text("INSERT INTO folder(id,name,workspace_id,parent_id) VALUES (1,'/',1,NULL),(2,'child',1,1)"))
        db.execute(text("INSERT INTO files(id,name,file_path,file_size,status,workspace_id,uploader_id) VALUES (1,'original','original',3,'success',1,1)"))
    monkeypatch.setattr(server, 'SessionLocal', sessionmaker(bind=postgres))
    arguments = {
        'create_folder': {'name': 'new'}, 'move_file': {'file_id': 1, 'new_name': 'new'},
        'delete_file': {'file_id': 1}, 'move_folder': {'folder_id': 2, 'new_name': 'new'},
        'delete_folder': {'folder_id': 2}, 'batch_delete': {'items': [server.BatchDeleteItem(id=1)]},
        'upload_file': {'filename': 'new.txt', 'content': 'abc'},
    }[operation]
    owner, method = {
        'create_folder': (server.folder_service, 'create_folder'),
        'move_file': (server.file_service, 'update_file'),
        'delete_file': (server.file_service, 'delete_file'),
        'move_folder': (server.folder_service, 'update_folder'),
        'delete_folder': (server.folder_service, 'delete_folder'),
        'batch_delete': (server.file_service, 'delete_file'),
        'upload_file': (server.file_service, 'create_uploaded_file'),
    }[operation]
    calls = []
    def dispatch(session, *args, **kwargs):
        calls.append(True)
        with postgres.connect() as independent:
            independent.execute(text("SET LOCAL lock_timeout='100ms'"))
            with pytest.raises(DBAPIError) as error:
                independent.execute(text("UPDATE workspace_members SET role='viewer' WHERE workspace_id=1 AND user_id=1"))
            assert error.value.orig.pgcode == '55P03'
            independent.rollback()
        raise BusinessRuleError('Intentional transaction boundary stop')
    monkeypatch.setattr(owner, method, dispatch)
    token = set_mcp_context(McpRequestContext(1, 1, 'admin'))
    try:
        result = json.loads(await getattr(server, operation)(**arguments))
        errors = [entry['error'] for entry in result['errors']] if operation == 'batch_delete' else [result['error']]
        assert errors == ['Intentional transaction boundary stop']
        with postgres.begin() as independent:
            independent.execute(text("UPDATE workspace_members SET role='viewer' WHERE workspace_id=1 AND user_id=1"))
        result = json.loads(await getattr(server, operation)(**arguments))
        errors = [entry['error'] for entry in result['errors']] if operation == 'batch_delete' else [result['error']]
        assert errors == ['需要编辑者或管理员权限']
        assert calls == [True]
        with postgres.connect() as db:
            assert db.execute(text('SELECT id,name FROM files')).all() == [(1, 'original')]
            assert db.execute(text('SELECT id,name FROM folder ORDER BY id')).all() == [(1, '/'), (2, 'child')]
            assert db.scalar(text('SELECT count(*) FROM upload_reservations')) == 0
    finally:
        reset_mcp_context(token)


async def test_share_publication_locks_membership_and_file_and_preserves_owner_cancellation(postgres):
    import httpx
    from types import SimpleNamespace
    from fastapi import FastAPI
    from sqlalchemy.exc import DBAPIError
    from app.features.share import router, service
    from app.api.dependencies import get_current_user
    from app.infra.extensions import get_session_factory
    from app.exceptions import register_exception_handlers
    upgrade(postgres)
    with postgres.begin() as db:
        db.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (1,'actor','x','common')"))
        db.execute(text("INSERT INTO workspaces(id,name,owner_id) VALUES (1,'one',1)"))
        db.execute(text("INSERT INTO workspace_members(workspace_id,user_id,role) VALUES (1,1,'viewer')"))
        db.execute(text("INSERT INTO files(id,name,file_path,file_size,status,workspace_id,uploader_id) VALUES (1,'original','original',3,'success',1,1)"))
    factory = sessionmaker(bind=postgres)
    with factory() as owned:
        share = service.create_share_link(owned, 1, 1, commit=False)
        for statement in ['DELETE FROM workspace_members WHERE workspace_id=1 AND user_id=1',
                          "UPDATE files SET name='concurrent' WHERE id=1", 'DELETE FROM files WHERE id=1']:
            with postgres.connect() as independent:
                independent.execute(text("SET LOCAL lock_timeout='100ms'"))
                with pytest.raises(DBAPIError) as error:
                    independent.execute(text(statement))
                assert error.value.orig.pgcode == '55P03'
                independent.rollback()
        owned.commit()
        share_id, token = share.id, share.token
    with postgres.begin() as independent:
        independent.execute(text('DELETE FROM workspace_members WHERE workspace_id=1 AND user_id=1'))
    app = FastAPI(); register_exception_handlers(app); app.include_router(router.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[get_session_factory] = lambda: factory
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
        assert (await client.post('/share', json={'file_id': 1})).status_code == 403
        mine = await client.get('/share/my')
        assert mine.status_code == 200 and mine.json()['data'][0]['id'] == share_id
        assert (await client.delete('/share/' + str(share_id))).status_code == 200
        assert (await client.get('/share/' + token)).status_code == 404
    with postgres.connect() as db:
        assert db.scalar(text('SELECT count(*) FROM shares')) == 0
        assert db.scalar(text('SELECT count(*) FROM files')) == 1


async def test_mcp_share_publication_rejects_concurrent_workspace_change(postgres, monkeypatch):
    upgrade(postgres)
    with postgres.begin() as db:
        db.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (1,'actor','x','common')"))
        db.execute(text("INSERT INTO workspaces(id,name,owner_id) VALUES (1,'selected',1),(2,'other',1)"))
        db.execute(text("INSERT INTO workspace_members(workspace_id,user_id,role) VALUES (1,1,'viewer'),(2,1,'viewer')"))
        db.execute(text("INSERT INTO files(id,name,file_path,file_size,status,workspace_id,uploader_id) VALUES (1,'original','original',3,'success',1,1)"))
    monkeypatch.setattr(server, 'SessionLocal', sessionmaker(bind=postgres))
    authorized = server.file_service.get_authorized_file
    def move_after_read(session, **kwargs):
        file = authorized(session, **kwargs)
        with postgres.begin() as independent:
            independent.execute(text('UPDATE files SET workspace_id=2 WHERE id=1'))
        return file
    monkeypatch.setattr(server.file_service, 'get_authorized_file', move_after_read)
    token = set_mcp_context(McpRequestContext(1, 1, 'viewer'))
    try:
        result = json.loads(await server.get_file_download_url(1))
        assert result == {'error': 'Permission denied'}
        with postgres.connect() as db:
            assert db.scalar(text('SELECT workspace_id FROM files WHERE id=1')) == 2
            assert db.scalar(text('SELECT count(*) FROM shares')) == 0
    finally:
        reset_mcp_context(token)


@pytest.mark.parametrize('change', ['demote', 'delete'])
async def test_settings_owned_routes_reject_stale_admin_with_warm_cache(postgres, infrastructure, monkeypatch, change):
    import httpx
    from types import SimpleNamespace
    from fastapi import FastAPI
    from app.api.dependencies import require_admin
    from app.features.sys_dict import router
    from app.infra.extensions import get_session_factory
    from app.infra import cache
    from app.exceptions import register_exception_handlers
    upgrade(postgres)
    with postgres.begin() as db:
        db.execute(text("DELETE FROM sys_dict"))
        db.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (1,'actor','x','admin')"))
        db.execute(text("INSERT INTO sys_dict(id,key,value,des,enable) VALUES (1,'original','value','description',true)"))
    monkeypatch.setattr(cache, 'redis_client', infrastructure['redis'])
    app = FastAPI(); register_exception_handlers(app); app.include_router(router.router)
    # Explicit stale identity seam; current SQL authorization is real.
    app.dependency_overrides[require_admin] = lambda: SimpleNamespace(id=1, role='admin')
    app.dependency_overrides[get_session_factory] = lambda: sessionmaker(bind=postgres)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
        assert (await client.get('/sys_dicts')).status_code == 200
        assert (await client.get('/sys_dicts/1')).status_code == 200
        assert infrastructure['redis'].exists('sys_dict:all:all') == 1
        with postgres.begin() as independent:
            independent.execute(text("UPDATE users SET role='common' WHERE id=1" if change == 'demote' else 'DELETE FROM users WHERE id=1'))
        for method, path, body in [
            ('GET','/sys_dicts',None), ('GET','/sys_dicts/1',None),
            ('POST','/sys_dicts',{'key':'new','value':'forbidden'}),
            ('PUT','/sys_dicts/1',{'key':'changed','value':'forbidden'}),
            ('DELETE','/sys_dicts/1',None),
        ]:
            response = await client.request(method, path, json=body)
            assert response.status_code == 403, response.text
    with postgres.connect() as db:
        assert db.execute(text('SELECT id,key,value,des,enable FROM sys_dict')).all() == [(1,'original','value','description',True)]


async def test_settings_http_write_keeps_actor_lock_until_commit(postgres, monkeypatch):
    import httpx
    from types import SimpleNamespace
    from fastapi import FastAPI
    from sqlalchemy.exc import DBAPIError
    from app.api.dependencies import require_admin
    from app.features.sys_dict import router
    from app.infra.extensions import get_session_factory
    from app.exceptions import register_exception_handlers
    upgrade(postgres)
    with postgres.begin() as db:
        db.execute(text("DELETE FROM sys_dict"))
        db.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (1,'actor','x','admin')"))
    create = router.sys_dict_service.create_sys_dict
    observed = []
    def observe_then_create(session, data):
        with postgres.connect() as independent:
            independent.execute(text("SET LOCAL lock_timeout='100ms'"))
            with pytest.raises(DBAPIError) as error:
                independent.execute(text("UPDATE users SET role='common' WHERE id=1"))
            assert error.value.orig.pgcode == '55P03'
            independent.rollback()
        observed.append(True)
        return create(session, data)
    monkeypatch.setattr(router.sys_dict_service, 'create_sys_dict', observe_then_create)
    monkeypatch.setattr(router.sys_dict_service, '_invalidate_sys_dict_cache', lambda: None)
    app = FastAPI(); register_exception_handlers(app); app.include_router(router.router)
    app.dependency_overrides[require_admin] = lambda: SimpleNamespace(id=1, role='admin')
    app.dependency_overrides[get_session_factory] = lambda: sessionmaker(bind=postgres)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
        assert (await client.post('/sys_dicts', json={'key':'allowed','value':'one'})).status_code == 201
        assert observed == [True]
        with postgres.begin() as independent:
            independent.execute(text("UPDATE users SET role='common' WHERE id=1"))
        response = await client.post('/sys_dicts', json={'key':'denied','value':'two'})
        assert response.status_code == 403, response.text
        assert observed == [True]
    with postgres.connect() as db:
        assert db.execute(text('SELECT key,value FROM sys_dict')).all() == [('allowed','one')]


async def test_inbox_owned_http_routes_preserve_message_ownership(postgres):
    import httpx
    from types import SimpleNamespace
    from fastapi import FastAPI
    from app.api.dependencies import get_current_user
    from app.features.inbox import router
    from app.infra.extensions import get_session_factory
    from app.exceptions import register_exception_handlers
    upgrade(postgres)
    with postgres.begin() as db:
        db.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (1,'actor','x','common'),(2,'other','x','common')"))
        db.execute(text("INSERT INTO inbox(id,user_id,content,is_read,is_deleted) VALUES (1,1,'own',false,false),(2,2,'private',false,false)"))
    app = FastAPI(); register_exception_handlers(app); app.include_router(router.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[get_session_factory] = lambda: sessionmaker(bind=postgres)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
        listing = await client.get('/inbox')
        assert listing.status_code == 200 and [row['id'] for row in listing.json()['data']['items']] == [1]
        assert (await client.put('/inbox/2/read')).status_code == 404
        assert (await client.delete('/inbox/2')).status_code == 404
        read = await client.put('/inbox/1/read')
        assert read.status_code == 200 and read.json()['data']['is_read'] is True
        assert (await client.put('/inbox/read-all')).status_code == 200
        deleted = await client.delete('/inbox/1')
        assert deleted.status_code == 204 and deleted.content == b''
        assert (await client.get('/inbox')).json()['data']['items'] == []
    with postgres.connect() as db:
        assert db.execute(text('SELECT id,is_read,is_deleted FROM inbox ORDER BY id')).all() == [(1,True,True),(2,False,False)]


@pytest.mark.parametrize('change', ['demote', 'delete'])
async def test_usage_owned_http_rejects_stale_admin_and_preserves_self_reads(postgres, change):
    import httpx
    from types import SimpleNamespace
    from fastapi import FastAPI
    from app.api.dependencies import get_current_user, require_admin
    from app.features.token_usage import router
    from app.infra.extensions import get_session_factory
    from app.exceptions import register_exception_handlers
    upgrade(postgres)
    with postgres.begin() as db:
        db.execute(text("INSERT INTO users(id,username,password_hash,role,total_prompt_tokens,total_completion_tokens,total_tokens) VALUES (1,'actor','x','admin',2,3,5),(2,'other','x','common',7,11,18)"))
        db.execute(text("INSERT INTO token_usage_logs(user_id,action,prompt_tokens,completion_tokens,total_tokens,created_at) VALUES (1,'chat',2,3,5,now()),(2,'chat',7,11,18,now())"))
    app = FastAPI(); register_exception_handlers(app); app.include_router(router.router)
    # Explicit stale authenticated identity; permissions and aggregate SQL are real.
    identity = SimpleNamespace(id=1, role='admin')
    app.dependency_overrides[get_current_user] = lambda: identity
    app.dependency_overrides[require_admin] = lambda: identity
    app.dependency_overrides[get_session_factory] = lambda: sessionmaker(bind=postgres)
    admin_paths = ['/admin/token-usage/users', '/admin/token-usage/logs',
                   '/admin/token-usage/daily', '/admin/token-usage/daily/per-user']
    foreign_paths = ['/token-usage/'+kind+'/2' for kind in ['stats','logs','daily']]
    self_paths = ['/token-usage/'+kind+suffix for kind in ['stats','logs','daily'] for suffix in ['', '/1']]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
        for path in admin_paths + foreign_paths + self_paths:
            first = await client.get(path)
            assert first.status_code == 200, first.text
        assert (await client.get('/admin/token-usage/daily')).json()['data'][0]['total_tokens'] == 23
        assert (await client.get('/token-usage/stats/2')).json()['data']['total_tokens'] == 18
        with postgres.begin() as independent:
            independent.execute(text("UPDATE users SET role='common' WHERE id=1" if change == 'demote' else 'DELETE FROM users WHERE id=1'))
        for path in admin_paths + foreign_paths:
            response = await client.get(path)
            assert response.status_code == 403, response.text
        for path in self_paths:
            response = await client.get(path)
            assert response.status_code == (200 if change == 'demote' else 403), response.text
        if change == 'demote':
            assert (await client.get('/token-usage/stats')).json()['data']['total_tokens'] == 5
            assert [row['user_id'] for row in (await client.get('/token-usage/logs')).json()['data']['items']] == [1]
            assert (await client.get('/token-usage/daily')).json()['data'][0]['total_tokens'] == 5
    with postgres.connect() as db:
        assert db.execute(text('SELECT user_id,total_tokens FROM token_usage_logs ORDER BY user_id')).all() == ([(1,5),(2,18)] if change == 'demote' else [(2,18)])


@pytest.mark.parametrize('change', ['demote', 'remove'])
async def test_assistant_owned_http_rechecks_committed_membership(postgres, change):
    import httpx
    from types import SimpleNamespace
    from fastapi import FastAPI
    from app.api.dependencies import get_current_user, get_current_workspace
    from app.features.assistant import router, service
    from app.infra.extensions import get_session_factory
    from app.models.workspace import Workspace
    from app.exceptions import register_exception_handlers
    upgrade(postgres)
    with postgres.begin() as db:
        db.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (1,'actor','x','common')"))
        db.execute(text("INSERT INTO workspaces(id,name,owner_id) VALUES (1,'one',1)"))
        db.execute(text("INSERT INTO workspace_members(workspace_id,user_id,role) VALUES (1,1,'editor')"))
    factory = sessionmaker(bind=postgres)
    mode = 'expert' if change == 'demote' else 'fast'
    with factory() as owned:
        workspace = owned.get(Workspace, 1)
        conversation = service.create_conversation(owned, workspace, 1, mode, 'original')
        cid = conversation.id
        _, run, _ = service.prepare_run(owned, workspace, 1, cid, 'original query')
        run.status = 'waiting_permission'; run.pending_permission_id = 'p1'
        owned.commit(); rid = run.id
        source = service.create_conversation(owned, workspace, 1, 'fast', 'source')
        sid = source.id
    app = FastAPI(); register_exception_handlers(app); app.include_router(router.router)
    # Explicit identity/workspace snapshots; all service SQL and permission checks are real.
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[get_current_workspace] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[get_session_factory] = lambda: factory
    calls = [
        ('POST','/assistant/conversations',{'mode':mode}),
        ('GET','/assistant/conversations?mode='+mode,None),
        ('PATCH',f'/assistant/conversations/{cid}',{'title':'forbidden'}),
        ('DELETE',f'/assistant/conversations/{cid}',None),
        ('GET',f'/assistant/conversations/{cid}/messages',None),
        ('GET',f'/assistant/conversations/{cid}/active-run',None),
        ('POST',f'/assistant/conversations/{cid}/messages/stream',{'query':'forbidden'}),
        ('POST',f'/assistant/runs/{rid}/cancel',None),
        ('GET','/assistant/runs/active',None),
        ('GET',f'/assistant/runs/{rid}',None),
        ('POST',f'/assistant/runs/{rid}/permissions/p1',{'response':'once'}),
        ('POST',f'/assistant/conversations/{sid}/handoff',{}),
    ]
    with postgres.begin() as independent:
        independent.execute(text("UPDATE workspace_members SET role='viewer' WHERE user_id=1" if change=='demote' else 'DELETE FROM workspace_members WHERE user_id=1'))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
        for method, path, body in calls:
            response = await client.request(method, path, json=body)
            assert response.status_code == 403, (path,response.text)
        if change == 'demote':
            listing = await client.get('/assistant/conversations')
            assert listing.status_code == 200
            assert [row['id'] for row in listing.json()['data']['conversations']] == [sid]
    with postgres.connect() as db:
        assert db.execute(text('SELECT title FROM assistant_conversations ORDER BY id')).scalars().all() == ['original','source']
        assert db.execute(text('SELECT cancel_requested,pending_permission_id,permission_response FROM assistant_runs')).all() == [(False,'p1',None)]
        assert db.scalar(text('SELECT count(*) FROM assistant_messages')) == 2


@pytest.mark.parametrize('operation', ['prepare', 'archive', 'delete'])
def test_assistant_recovery_retains_member_lock_until_outer_write_commit(postgres, monkeypatch, operation):
    from datetime import timedelta
    from sqlalchemy.exc import DBAPIError
    from app.features.assistant import service
    from app.models.workspace import Workspace
    from app.infra.datetime_utils import utc_now
    upgrade(postgres)
    with postgres.begin() as db:
        db.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (1,'actor','x','common')"))
        db.execute(text("INSERT INTO workspaces(id,name,owner_id) VALUES (1,'one',1)"))
        db.execute(text("INSERT INTO workspace_members(workspace_id,user_id,role) VALUES (1,1,'editor')"))
    factory = sessionmaker(bind=postgres)
    with factory() as owned:
        workspace = owned.get(Workspace, 1)
        conversation = service.create_conversation(owned, workspace, 1, 'expert', 'original')
        cid = conversation.id
        _, old, _ = service.prepare_run(owned, workspace, 1, cid, 'old query')
        old.started_at = utc_now() - timedelta(minutes=5)
        old.metadata_dict = {}; owned.commit()
    recover = service._recover_abandoned_run
    observed = []
    def observe_recovery(session, run, *, commit=True):
        assert commit is False
        result = recover(session, run, commit=commit)
        if result:
            with postgres.connect() as independent:
                independent.execute(text("SET LOCAL lock_timeout='100ms'"))
                with pytest.raises(DBAPIError) as error:
                    independent.execute(text("UPDATE workspace_members SET role='viewer' WHERE user_id=1"))
                assert error.value.orig.pgcode == '55P03'
                independent.rollback()
            observed.append(True)
        return result
    monkeypatch.setattr(service, '_recover_abandoned_run', observe_recovery)
    with factory() as owned:
        workspace = owned.get(Workspace, 1)
        if operation == 'prepare': service.prepare_run(owned, workspace, 1, cid, 'replacement')
        elif operation == 'archive': service.update_conversation(owned, workspace, 1, cid, status='archived')
        else: service.delete_conversation(owned, workspace, 1, cid)
    assert observed == [True]
    with postgres.begin() as independent:
        independent.execute(text("UPDATE workspace_members SET role='viewer' WHERE user_id=1"))
        statuses = independent.execute(text('SELECT status FROM assistant_runs ORDER BY id')).scalars().all()
        assert statuses == (['failed','running'] if operation == 'prepare' else ['failed'] if operation == 'archive' else [])


@pytest.mark.parametrize('change', ['demote','remove','stop','revoke','delete-user'])
async def test_runtime_model_owned_dependency_rejects_independent_revocation(postgres, change):
    import httpx
    from fastapi import Depends, FastAPI
    from app.features.runtime_llm import router, tokens
    from app.infra.extensions import get_session_factory
    from app.models.opencode_runtime import OpenCodeRuntime
    upgrade(postgres)
    with postgres.begin() as db:
        db.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (1,'actor','x','common')"))
        db.execute(text("INSERT INTO workspaces(id,name,owner_id) VALUES (1,'one',1)"))
        db.execute(text("INSERT INTO workspace_members(workspace_id,user_id,role) VALUES (1,1,'editor')"))
    factory = sessionmaker(bind=postgres)
    with factory() as owned:
        runtime = OpenCodeRuntime(workspace_id=1, user_id=1, status='running')
        owned.add(runtime); owned.commit()
        raw = tokens.issue_token(owned, runtime)
    app = FastAPI()
    app.dependency_overrides[get_session_factory] = lambda: factory
    # Production authentication dependency with actual signed/hash-bound token.
    # Probe avoids a provider request; it proves dependency admission only.
    @app.get('/credential')
    async def credential(payload=Depends(router.current_credential)):
        return {'user_id':int(payload['sub']), 'workspace_id':payload['workspace_id']}
    statements = {
        'demote': "UPDATE workspace_members SET role='viewer' WHERE user_id=1",
        'remove': 'DELETE FROM workspace_members WHERE user_id=1',
        'stop': "UPDATE opencode_runtimes SET status='stopped' WHERE user_id=1",
        'revoke': 'UPDATE mcp_runtime_tokens SET revoked_at=now() WHERE user_id=1',
        'delete-user': 'DELETE FROM users WHERE id=1',
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
        headers = {'Authorization':'Bearer '+raw}
        first = await client.get('/credential', headers=headers)
        assert first.status_code == 200 and first.json() == {'user_id':1,'workspace_id':1}
        with postgres.begin() as independent: independent.execute(text(statements[change]))
        denied = await client.get('/credential', headers=headers)
        assert denied.status_code == 401
        assert raw not in denied.text


@pytest.mark.parametrize('phase', ['sql-reject','rollback-fault','commit-ack','close-fault'])
def test_accounting_faults_preserve_business_and_recover_actual_postgres(postgres, monkeypatch, phase):
    from sqlalchemy.orm import Session
    from app.features.token_usage import service
    upgrade(postgres)
    with postgres.begin() as db:
        db.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (1,'actor','x','common')"))
    active, calls, logged = [True], [], []
    private = 'private-accounting-query'
    class FaultSession(Session):
        def commit(self):
            super().commit()
            if active[0] and phase == 'commit-ack': raise RuntimeError(private)
        def rollback(self):
            calls.append('rollback')
            if active[0] and phase == 'rollback-fault': raise RuntimeError(private)
            return super().rollback()
        def close(self):
            calls.append('close')
            if active[0] and phase == 'close-fault': raise RuntimeError(private)
            return super().close()
        def invalidate(self):
            calls.append('invalidate')
            return super().invalidate()
    monkeypatch.setattr(service, 'SessionLocal', sessionmaker(bind=postgres, class_=FaultSession))
    monkeypatch.setattr(service.logger, 'error', lambda message,*args: logged.append(message.format(*args)))
    # SQL rejection is native varchar(50) enforcement after the aggregate UPDATE.
    action = 'x'*51 if phase in {'sql-reject','rollback-fault'} else 'chat'
    assert service.record_usage(1, action, prompt_tokens=2, completion_tokens=3, query_summary=private) is None
    assert logged and private not in str(logged)
    assert 'close' in calls
    if phase in {'rollback-fault','close-fault'}: assert 'invalidate' in calls
    initial = 0 if phase in {'sql-reject','rollback-fault'} else 5
    with postgres.connect() as db:
        assert db.scalar(text('SELECT total_tokens FROM users WHERE id=1')) == initial
        assert db.scalar(text('SELECT count(*) FROM token_usage_logs')) == (0 if initial == 0 else 1)
    # A later accounting call succeeds on the same real engine/pool without retries.
    active[0] = False
    assert service.record_usage(1, 'chat', prompt_tokens=2, completion_tokens=3) is None
    with postgres.connect() as db:
        assert db.scalar(text('SELECT total_tokens FROM users WHERE id=1')) == initial + 5
        assert db.scalar(text('SELECT sum(total_tokens) FROM token_usage_logs')) == initial + 5


@pytest.mark.parametrize('outcome', ['done','engine-error','creation-error','cancel'])
async def test_stream_close_fault_preserves_actual_postgres_outcome(postgres, monkeypatch, outcome):
    import threading
    from sqlalchemy.orm import Session
    from app.features.assistant import service
    from app.features.assistant.event_protocol import AssistantEvent
    from app.models.workspace import Workspace
    from app.exceptions import ResourceNotFoundError
    upgrade(postgres)
    with postgres.begin() as db:
        db.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (1,'actor','x','common')"))
        db.execute(text("INSERT INTO workspaces(id,name,owner_id) VALUES (1,'one',1)"))
        db.execute(text("INSERT INTO workspace_members(workspace_id,user_id,role) VALUES (1,1,'editor')"))
    with sessionmaker(bind=postgres)() as owned:
        workspace = owned.get(Workspace, 1)
        conversation = service.create_conversation(owned, workspace, 1, 'fast', 'original')
        cid = conversation.id
        _, run, history = service.prepare_run(owned, workspace, 1, cid, 'query')
        rid, mid = run.id, run.assistant_message_id
    loop_thread = threading.get_ident()
    calls, logged = [], []
    class FaultSession(Session):
        def __init__(self, *args, **kwargs):
            self.owner = threading.get_ident()
            assert self.owner != loop_thread
            super().__init__(*args, **kwargs)
        def close(self):
            assert threading.get_ident() == self.owner
            calls.append('close')
            raise RuntimeError('private-connection-url')
        def invalidate(self):
            assert threading.get_ident() == self.owner
            calls.append('invalidate')
            return super().invalidate()
    entered = asyncio.Event()
    class Engine:
        async def stream(self, **kwargs):
            yield AssistantEvent('token', {'content':'Answer'})
            if outcome == 'engine-error': raise RuntimeError('private-provider-url')
            if outcome == 'cancel':
                entered.set()
                await asyncio.Event().wait()
    monkeypatch.setattr(service, 'FastEngine', Engine)
    monkeypatch.setattr(service.logger, 'error', lambda message,*args: logged.append(message.format(*args)))
    factory = sessionmaker(bind=postgres, class_=FaultSession)
    async def collect():
        return [event async for event in service.stream_run(factory, 1, 1,
                999 if outcome == 'creation-error' else cid, rid, history)]
    if outcome == 'creation-error':
        with pytest.raises(ResourceNotFoundError): await collect()
    elif outcome == 'cancel':
        task = asyncio.create_task(collect())
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    else:
        events = await collect()
        assert events[-1].type == ('done' if outcome == 'done' else 'error')
        assert 'private-' not in str(events)
    assert calls == ['close','invalidate']
    assert logged and 'private-' not in str(logged)
    expected = {'done':'completed','engine-error':'failed','cancel':'cancelled','creation-error':'running'}[outcome]
    with postgres.connect() as db:
        assert db.scalar(text('SELECT status FROM assistant_runs WHERE id=:id'), {'id':rid}) == expected
        row = db.execute(text('SELECT status,content FROM assistant_messages WHERE id=:id'), {'id':mid}).one()
        assert row.status == ('streaming' if outcome == 'creation-error' else expected)
        if outcome in {'done','engine-error'}: assert row.content == 'Answer'
    assert service.run_registry.get(rid) is None


@pytest.mark.parametrize('quiet', [False,True])
async def test_legacy_chat_http_stops_after_committed_member_removal(postgres, monkeypatch, quiet):
    import httpx
    from types import SimpleNamespace
    from fastapi import FastAPI
    from app.api.dependencies import get_current_user,get_current_workspace
    from app.features.chat import router
    from app.infra.extensions import get_session_factory
    upgrade(postgres)
    with postgres.begin() as db:
        db.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (1,'actor','x','common')"))
        db.execute(text("INSERT INTO workspaces(id,name,owner_id) VALUES (1,'one',1)"))
        db.execute(text("INSERT INTO workspace_members(workspace_id,user_id,role) VALUES (1,1,'viewer')"))
    app=FastAPI();app.include_router(router.router)
    app.dependency_overrides[get_current_user]=lambda:SimpleNamespace(id=1)
    app.dependency_overrides[get_current_workspace]=lambda:SimpleNamespace(id=1)
    app.dependency_overrides[get_session_factory]=lambda:sessionmaker(bind=postgres)
    entered,closed=asyncio.Event(),asyncio.Event()
    async def model(*args,**kwargs):
        try:
            yield 'data: {"type":"token","content":"allowed"}\n\n'
            entered.set()
            if quiet:await asyncio.Event().wait()
            else:
                with postgres.begin() as independent:
                    independent.execute(text('DELETE FROM workspace_members WHERE user_id=1'))
                yield 'data: {"type":"token","content":"private-late-data"}\n\n'
        finally:closed.set()
    monkeypatch.setattr(router,'generate_chat_events',model)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test') as client:
        pending=asyncio.create_task(client.post('/chat',json={'query':'question'}))
        if quiet:
            await asyncio.wait_for(entered.wait(),3)
            with postgres.begin() as independent:
                independent.execute(text('DELETE FROM workspace_members WHERE user_id=1'))
        response=await asyncio.wait_for(pending,5)
    assert response.status_code==200
    assert 'allowed' in response.text and 'private-late-data' not in response.text
    payloads=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
    assert any('权限已撤销' in payload.get('content','') for payload in payloads)
    assert closed.is_set()
