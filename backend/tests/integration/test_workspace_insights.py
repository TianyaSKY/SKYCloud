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


def warmed(redis, prefix):
    """The synchronous read must have cached its result under this prefix.

    Cache keys carry a durable SQL revision suffix, so only the prefix is asserted.
    """
    return bool(redis.keys(prefix + '*'))

pytestmark = pytest.mark.skipif(os.getenv("RUN_WORKSPACE_INSIGHTS_TESTS") != "1",
                               reason="Opt-in isolated PostgreSQL workspace insights")


@pytest.mark.parametrize('mode', ['fast', 'expert'])
@pytest.mark.parametrize('reject_commit', [False, True])
async def test_disconnected_cancel_drains_native_sql_lock_and_signals_only_committed_run(postgres, reject_commit, mode):
    import threading
    import time
    from contextlib import contextmanager
    from types import SimpleNamespace
    import httpx
    from fastapi import FastAPI
    from app.api.dependencies import get_current_user, get_current_workspace
    from app.features.assistant import router, service
    from app.infra.extensions import get_session_factory
    from app.models.workspace import Workspace
    upgrade(postgres)
    with postgres.begin() as db:
        db.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (1,'cancel-owner','x','common')"))
        db.execute(text("INSERT INTO workspaces(id,name,owner_id) VALUES (1,'cancel-space',1)"))
        db.execute(text("INSERT INTO workspace_members(workspace_id,user_id,role) VALUES (1,1,'editor')"))
    factory = sessionmaker(bind=postgres)
    with factory() as db:
        workspace = db.get(Workspace, 1)
        conversation = service.create_conversation(db, workspace, 1, mode, 'cancel')
        _, run, _ = service.prepare_run(db, workspace, 1, conversation.id, 'cancel query')
        run_id = run.id
    if reject_commit:
        with postgres.begin() as db:
            db.execute(text('ALTER TABLE assistant_runs ADD CONSTRAINT reject_cancel CHECK (cancel_requested=false)'))
    entered, closed = threading.Event(), threading.Event()
    worker_pids = []
    @contextmanager
    def owned_factory():
        with factory() as db:
            worker_pids.append(db.scalar(text('SELECT pg_backend_pid()')))
            entered.set()
            try:
                yield db
            finally:
                closed.set()
    app = FastAPI()
    app.include_router(router.router)
    # Only HTTP identity snapshots are supplied; authorization, cancellation
    # writes, commit rejection and registry signaling use production code.
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[get_current_workspace] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[get_session_factory] = lambda: owned_factory
    controller = service.run_registry.register(run_id)
    lock = postgres.connect()
    transaction = lock.begin()
    lock.execute(text('LOCK TABLE assistant_runs IN ACCESS EXCLUSIVE MODE'))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            pending = asyncio.create_task(client.post(f'/assistant/runs/{run_id}/cancel'))
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                deadline = time.monotonic() + 2
                while True:
                    with postgres.connect() as observer:
                        waiting = observer.scalar(text("SELECT wait_event_type='Lock' FROM pg_stat_activity WHERE pid=:pid"), {'pid': worker_pids[0]})
                    if waiting:
                        break
                    assert time.monotonic() < deadline, 'Cancellation worker did not reach the held SQL lock'
                    await asyncio.sleep(.01)
                pending.cancel('client-disconnected'); await asyncio.sleep(.01)
                pending.cancel('repeat-disconnect'); await asyncio.sleep(.01)
                assert not pending.done() and not closed.is_set() and not controller.cancel_event.is_set()
            finally:
                transaction.rollback()
                with pytest.raises(asyncio.CancelledError) as error:
                    await asyncio.wait_for(pending, 5)
            assert str(error.value) == 'client-disconnected'
        assert closed.is_set() and controller.cancel_event.is_set() == (not reject_commit)
        with postgres.connect() as observer:
            assert observer.scalar(text('SELECT cancel_requested FROM assistant_runs WHERE id=:id'), {'id': run_id}) == (not reject_commit)
    finally:
        if transaction.is_active:
            transaction.rollback()
        lock.close()
        service.run_registry.unregister(run_id)


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
            assert warmed(infrastructure['redis'], 'search:fuzzy:1:sql:')
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
            assert warmed(infrastructure['redis'], 'search:fuzzy:1:sql:')
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
        assert warmed(infrastructure['redis'], 'user:profile:1:sql:')
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
        assert warmed(infrastructure['redis'], user_service.USER_CACHE_PREFIX+':2:sql:')
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
                assert warmed(infrastructure['redis'], prefix + ':1:sql:')
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
    from app.features.sys_dict import service as sys_dict_service
    from app.infra.extensions import get_session_factory
    from app.infra import cache
    from app.exceptions import register_exception_handlers
    upgrade(postgres)
    with postgres.begin() as db:
        db.execute(text("DELETE FROM sys_dict"))
        db.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (1,'actor','x','admin')"))
        db.execute(text("INSERT INTO sys_dict(id,key,value,des,enable) VALUES (1,'original','value','description',true)"))
    monkeypatch.setattr(cache, 'redis_client', infrastructure['redis'])
    # Warm the shared cache through the cached read itself: the HTTP route locks the
    # actor row for authorization, and a row-locking transaction is deliberately
    # excluded from the shared cache, so it can never be the warming step.
    with sessionmaker(bind=postgres)() as warmer:
        sys_dict_service.get_sys_dict_all_sync(warmer)
    assert warmed(infrastructure['redis'], 'sys_dict:all:all')
    app = FastAPI(); register_exception_handlers(app); app.include_router(router.router)
    # Explicit stale identity seam; current SQL authorization is real.
    app.dependency_overrides[require_admin] = lambda: SimpleNamespace(id=1, role='admin')
    app.dependency_overrides[get_session_factory] = lambda: sessionmaker(bind=postgres)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
        assert (await client.get('/sys_dicts')).status_code == 200
        assert (await client.get('/sys_dicts/1')).status_code == 200
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


@pytest.mark.parametrize('leased,cleanup', [(False,None),(True,None),(False,'self-cancel'),(True,'self-cancel'),(False,'repeat-cancel'),(True,'repeat-cancel')])
async def test_prepared_response_header_failure_after_member_removal(postgres, leased, cleanup):
    from types import SimpleNamespace
    from starlette.requests import ClientDisconnect
    from app.features.assistant import repository, service
    from app.features.assistant.stream_response import PreparedStreamResponse
    upgrade(postgres)
    with postgres.begin() as db:
        db.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (1,'reader','x','common')"))
        db.execute(text("INSERT INTO workspaces(id,name,owner_id) VALUES (1,'one',1)"))
        db.execute(text("INSERT INTO workspace_members(workspace_id,user_id,role) VALUES (1,1,'admin')"))
    factory = sessionmaker(bind=postgres)
    with factory() as db:
        workspace = SimpleNamespace(id=1)
        conversation = service.create_conversation(db, workspace, 1, 'fast', 'test')
        _, run, _ = service.prepare_run(db, workspace, 1, conversation.id, 'question')
        scope = (1, 1, conversation.id, run.id)
        if leased:
            repository.touch_run_lease(db, run.id, 1)
            db.commit()
    with postgres.begin() as independent:
        independent.execute(text('DELETE FROM workspace_members WHERE workspace_id=1 AND user_id=1'))
    entered = False
    async def body():
        nonlocal entered
        entered = True
        yield b'unreachable'
    async def receive(): await asyncio.Event().wait()
    async def send(message): raise OSError('header send failed')
    import threading
    closing,release,closed=(asyncio.Event() for _ in range(3))
    loop_thread=threading.get_ident()
    sessions=[]
    iterator=body()
    class Body:
        def __aiter__(self):return self
        async def __anext__(self):return await anext(iterator)
        async def aclose(self):
            closing.set()
            if cleanup=='repeat-cancel':await release.wait()
            await iterator.aclose();closed.set()
            if cleanup=='self-cancel':raise asyncio.CancelledError('private-close-credential')
    def owned_factory():
        assert closed.is_set() and threading.get_ident()!=loop_thread
        session=factory();sessions.append(session);return session
    response = PreparedStreamResponse(Body(), session_factory=owned_factory, run_scope=scope)
    pending=asyncio.create_task(response({'type':'http','asgi':{'spec_version':'2.4'}},receive,send))
    try:
        await asyncio.wait_for(closing.wait(),2)
        if cleanup=='repeat-cancel':
            pending.cancel();await asyncio.sleep(0)
            pending.cancel();await asyncio.sleep(0.01)
            assert not pending.done() and not sessions
            with postgres.connect() as db:
                assert db.scalar(text('SELECT status FROM assistant_runs'))=='running'
    finally:
        release.set()
        with pytest.raises(ClientDisconnect):await asyncio.wait_for(pending,3)
    assert not entered and closed.is_set() and len(sessions)==1
    with postgres.connect() as db:
        assert db.execute(text('SELECT status,cancel_requested FROM assistant_runs')).one() == (
            'running' if leased else 'cancelled', not leased)
        assert db.scalar(text("SELECT status FROM assistant_messages WHERE role='assistant'")) == (
            'streaming' if leased else 'cancelled')


def test_prepared_compensation_serializes_with_native_lease_claim(postgres):
    from types import SimpleNamespace
    from sqlalchemy.exc import DBAPIError
    from app.features.assistant import repository, service
    upgrade(postgres)
    with postgres.begin() as db:
        db.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (1,'reader','x','common')"))
        db.execute(text("INSERT INTO workspaces(id,name,owner_id) VALUES (1,'one',1)"))
        db.execute(text("INSERT INTO workspace_members(workspace_id,user_id,role) VALUES (1,1,'admin')"))
    factory = sessionmaker(bind=postgres)
    with factory() as db:
        workspace = SimpleNamespace(id=1)
        conversation = service.create_conversation(db, workspace, 1, 'fast', 'test')
        _, run, _ = service.prepare_run(db, workspace, 1, conversation.id, 'question')
        scope = (1, 1, conversation.id, run.id)
        assert repository.cancel_unclaimed_run(db, *scope)
        with postgres.connect() as independent:
            independent.execute(text("SET lock_timeout='100ms'"))
            with pytest.raises(DBAPIError) as error:
                independent.execute(text("UPDATE assistant_runs SET metadata_json='{}' WHERE id=:id"), {'id': scope[-1]})
            assert error.value.orig.pgcode == '55P03'
            independent.rollback()
        db.commit()
    with factory() as independent:
        assert repository.touch_run_lease(independent, scope[-1], 2) is False


@pytest.mark.parametrize('fault', ['cancel-before-commit', 'cancel-after-commit', 'commit-rejected', 'ack-lost', 'close-failed'])
async def test_http_preparation_abort_compensates_after_owned_transaction(postgres, monkeypatch, fault):
    import threading
    from types import SimpleNamespace
    from sqlalchemy.orm import Session
    from app.features.assistant import router, service
    from app.features.assistant.schemas import AssistantMessageRequest
    upgrade(postgres)
    with postgres.begin() as db:
        db.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (1,'reader','x','common')"))
        db.execute(text("INSERT INTO workspaces(id,name,owner_id) VALUES (1,'one',1)"))
        db.execute(text("INSERT INTO workspace_members(workspace_id,user_id,role) VALUES (1,1,'admin')"))
    workspace = SimpleNamespace(id=1)
    with sessionmaker(bind=postgres)() as db:
        conversation = service.create_conversation(db, workspace, 1, 'fast', 'test')
        conversation_id = conversation.id
    loop_thread = threading.get_ident()
    entered, release = threading.Event(), threading.Event()
    opened, calls = [], []
    original = RuntimeError('controlled preparation fault')
    class OwnedSession(Session):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.owner = threading.get_ident()
            assert self.owner != loop_thread
            self.preparing = not opened
            opened.append(self)
            calls.append('prepare-open' if self.preparing else 'cleanup-open')
        def check(self): assert threading.get_ident() == self.owner
        def commit(self):
            self.check()
            if self.preparing and fault == 'cancel-before-commit':
                entered.set()
                assert release.wait(5)
            if self.preparing and fault == 'commit-rejected':
                raise original
            super().commit()
            calls.append('prepare-commit' if self.preparing else 'cleanup-commit')
            if self.preparing and fault == 'cancel-after-commit':
                entered.set()
                assert release.wait(5)
            if self.preparing and fault == 'ack-lost':
                raise original
        def close(self):
            self.check()
            calls.append('prepare-close' if self.preparing else 'cleanup-close')
            if self.preparing and fault == 'close-failed':
                raise original
            super().close()
        def invalidate(self):
            self.check()
            calls.append('prepare-invalidate' if self.preparing else 'cleanup-invalidate')
            super().invalidate()
    factory = sessionmaker(bind=postgres, class_=OwnedSession)
    pending = asyncio.create_task(router.stream_assistant_message(
        conversation_id, AssistantMessageRequest(query='question'), SimpleNamespace(headers={}),
        current_user=SimpleNamespace(id=1), workspace=workspace, session_factory=factory))
    if fault.startswith('cancel-'):
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            pending.cancel()
            await asyncio.sleep(0)
            pending.cancel()
            await asyncio.sleep(0)
            assert not pending.done() and len(opened) == 1
            with postgres.connect() as independent:
                assert independent.scalar(text('SELECT count(*) FROM assistant_runs')) == (fault == 'cancel-after-commit')
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 3)
    elif fault == 'close-failed':
        from starlette.requests import ClientDisconnect
        response = await pending
        assert calls[-1] == 'prepare-invalidate'
        async def receive(): await asyncio.Event().wait()
        async def send(message): raise OSError('header send failed')
        with pytest.raises(ClientDisconnect):
            await response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send)
    else:
        with pytest.raises(RuntimeError) as error:
            await pending
        assert error.value is original
    assert len(opened) == 2
    if fault != 'close-failed':
        assert opened[0].owner == opened[1].owner
    assert calls.index('prepare-close') < calls.index('cleanup-open')
    assert calls[-2:] == ['cleanup-commit', 'cleanup-close']
    with postgres.connect() as db:
        if fault == 'commit-rejected':
            assert db.scalar(text('SELECT count(*) FROM assistant_runs')) == 0
            assert db.scalar(text('SELECT count(*) FROM assistant_messages')) == 0
        else:
            assert db.execute(text('SELECT status,cancel_requested FROM assistant_runs')).one() == ('cancelled', True)
            assert db.execute(text('SELECT role,status FROM assistant_messages ORDER BY sequence')).all() == [
                ('user', 'completed'), ('assistant', 'cancelled')]


@pytest.mark.parametrize('fault', ['revoked', 'cancel'])
async def test_http_stream_initialization_abort_cleans_unclaimed_run(postgres, monkeypatch, fault):
    import threading
    from types import SimpleNamespace
    from sqlalchemy.orm import Session
    from app.exceptions import PermissionDeniedError
    from app.features.assistant import router, service
    from app.features.assistant.schemas import AssistantMessageRequest
    from app.models.workspace import Workspace
    upgrade(postgres)
    with postgres.begin() as db:
        db.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (1,'reader','x','common')"))
        db.execute(text("INSERT INTO workspaces(id,name,owner_id) VALUES (1,'one',1)"))
        db.execute(text("INSERT INTO workspace_members(workspace_id,user_id,role) VALUES (1,1,'admin')"))
    workspace = SimpleNamespace(id=1)
    with sessionmaker(bind=postgres)() as db:
        conversation = service.create_conversation(db, workspace, 1, 'fast', 'test')
        conversation_id = conversation.id
    entered, release = threading.Event(), threading.Event()
    opened, closed = [], []
    loop_thread = threading.get_ident()
    class OwnedSession(Session):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.owner = threading.get_ident()
            assert self.owner != loop_thread
            opened.append(self)
        def get(self, entity, *args, **kwargs):
            assert threading.get_ident() == self.owner
            if fault == 'cancel' and entity is Workspace:
                entered.set()
                assert release.wait(5)
            return super().get(entity, *args, **kwargs)
        def close(self):
            assert threading.get_ident() == self.owner
            super().close()
            closed.append(self)
    def forbidden_engine(*args, **kwargs): raise AssertionError('model must not start')
    monkeypatch.setattr(service, 'FastEngine', forbidden_engine)
    factory = sessionmaker(bind=postgres, class_=OwnedSession)
    response = await router.stream_assistant_message(
        conversation_id, AssistantMessageRequest(query='question'), SimpleNamespace(headers={}),
        current_user=SimpleNamespace(id=1), workspace=workspace, session_factory=factory)
    run_id = int(response.headers['X-Assistant-Run-Id'])
    if fault == 'revoked':
        with postgres.begin() as independent:
            independent.execute(text('DELETE FROM workspace_members WHERE workspace_id=1 AND user_id=1'))
    sent = []
    async def receive(): await asyncio.Event().wait()
    async def send(message): sent.append(message['type'])
    pending = asyncio.create_task(response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send))
    if fault == 'cancel':
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            pending.cancel()
            await asyncio.sleep(0)
            pending.cancel()
            await asyncio.sleep(0)
            assert not pending.done() and len(opened) == 2
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError): await asyncio.wait_for(pending, 3)
    else:
        with pytest.raises(PermissionDeniedError): await pending
    assert sent == ['http.response.start']
    assert len(opened) == 3 and closed == opened
    assert service.run_registry.get(run_id) is None
    with postgres.connect() as db:
        assert db.execute(text('SELECT status,cancel_requested FROM assistant_runs')).one() == ('cancelled', True)
        assert db.execute(text('SELECT role,status FROM assistant_messages ORDER BY sequence')).all() == [
            ('user', 'completed'), ('assistant', 'cancelled')]


@pytest.mark.parametrize('change,quiet,close_fault', [('version',False,None),('version',True,None),('family',False,None),('family',True,None),('mcp',False,None),('expiry',True,None),('version',False,'error'),('version',False,'cancel')])
async def test_legacy_stream_live_signed_session_revocation(postgres, infrastructure, monkeypatch, change, quiet, close_fault):
    import time
    import httpx
    import jwt
    from datetime import timedelta
    from fastapi import FastAPI
    from app.features.auth import refresh_sessions
    from app.features.chat import router
    from app.infra.extensions import SECRET_KEY, get_db, get_session_factory
    from app.infra.datetime_utils import utc_now
    from app.models.mcp_token import McpToken
    upgrade(postgres)
    with postgres.begin() as db:
        db.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (1,'reader','x','common')"))
        db.execute(text("INSERT INTO workspaces(id,name,owner_id) VALUES (1,'one',1)"))
        db.execute(text("INSERT INTO workspace_members(workspace_id,user_id,role) VALUES (1,1,'viewer')"))
    factory=sessionmaker(bind=postgres)
    monkeypatch.setattr(refresh_sessions, 'redis_client', infrastructure['redis'])
    claims={'sub':'1','ver':0,'exp':int(time.time())+(4 if change=='expiry' else 60)}
    if change=='family':
        with factory() as db:
            _, payload=refresh_sessions.create(db, claims)
            raw=payload['token']
    else:
        if change=='mcp': claims['type']='mcp'
        raw=jwt.encode(claims,SECRET_KEY,algorithm='HS256')
    if change=='mcp':
        with factory() as db:
            db.add(McpToken(user_id=1,token_hash=McpToken.hash_token(raw),token_preview='test',
                            expires_at=utc_now()+timedelta(minutes=1)))
            db.commit()
    def revoke():
        if change=='family': refresh_sessions.revoke_access('Bearer '+raw)
        elif change=='version':
            with postgres.begin() as db:
                db.execute(text('UPDATE users SET auth_version=auth_version+1 WHERE id=1'))
        elif change=='mcp':
            with postgres.begin() as db:
                db.execute(text('UPDATE mcp_tokens SET revoked_at=now() WHERE user_id=1'))
    app=FastAPI();app.include_router(router.router)
    # Real get_current_user / get_current_workspace, actual signed credential,
    # owned PostgreSQL factory. Only unused borrowed Session and model are seams.
    app.dependency_overrides[get_db]=lambda:None
    app.dependency_overrides[get_session_factory]=lambda:factory
    entered,closed=asyncio.Event(),asyncio.Event()
    async def model(*args,**kwargs):
        try:
            yield 'data: {"type":"token","content":"allowed"}\n\n'
            entered.set()
            if quiet: await asyncio.Event().wait()
            else:
                revoke()
                yield 'data: {"type":"token","content":"private-late-data"}\n\n'
        finally:
            closed.set()
            if close_fault:
                error=RuntimeError if close_fault=='error' else asyncio.CancelledError
                raise error('private-provider-close-credential')
    monkeypatch.setattr(router,'generate_chat_events',model)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test') as client:
        pending=asyncio.create_task(client.post('/chat',headers={'Authorization':'Bearer '+raw,'X-Workspace-Id':'1'},json={'query':'question'}))
        if quiet:
            await asyncio.wait_for(entered.wait(),3)
            revoke()
        response=await asyncio.wait_for(pending,8)
    assert response.status_code==200
    assert 'allowed' in response.text and 'private-late-data' not in response.text and raw not in response.text
    payloads=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
    assert any('登录状态已失效' in payload.get('content','') for payload in payloads)
    assert closed.is_set() and 'private-provider-close-credential' not in response.text
    with postgres.connect() as db:
        assert db.scalar(text('SELECT count(*) FROM workspace_members WHERE user_id=1'))==1


@pytest.mark.parametrize('change,quiet', [('version',False),('version',True),('family',False),('family',True),('mcp',False),('expiry',True)])
async def test_assistant_stream_live_signed_session_revocation(postgres, infrastructure, monkeypatch, change, quiet):
    import time
    import httpx
    import jwt
    from datetime import timedelta
    from types import SimpleNamespace
    from fastapi import FastAPI
    from app.features.auth import refresh_sessions
    from app.features.assistant import router, service
    from app.features.assistant.event_protocol import AssistantEvent
    from app.infra.extensions import SECRET_KEY, get_db, get_session_factory
    from app.infra.datetime_utils import utc_now
    from app.models.mcp_token import McpToken
    upgrade(postgres)
    with postgres.begin() as db:
        db.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (1,'reader','x','common')"))
        db.execute(text("INSERT INTO workspaces(id,name,owner_id) VALUES (1,'one',1)"))
        db.execute(text("INSERT INTO workspace_members(workspace_id,user_id,role) VALUES (1,1,'viewer')"))
    factory=sessionmaker(bind=postgres)
    with factory() as db:
        conversation=service.create_conversation(db,SimpleNamespace(id=1),1,'fast','test')
        cid=conversation.id
    monkeypatch.setattr(refresh_sessions, 'redis_client', infrastructure['redis'])
    claims={'sub':'1','ver':0,'exp':int(time.time())+(4 if change=='expiry' else 60)}
    if change=='family':
        with factory() as db:
            _, payload=refresh_sessions.create(db, claims)
            raw=payload['token']
    else:
        if change=='mcp': claims['type']='mcp'
        raw=jwt.encode(claims,SECRET_KEY,algorithm='HS256')
    if change=='mcp':
        with factory() as db:
            db.add(McpToken(user_id=1,token_hash=McpToken.hash_token(raw),token_preview='test',expires_at=utc_now()+timedelta(minutes=1)))
            db.commit()
    def revoke():
        if change=='family': refresh_sessions.revoke_access('Bearer '+raw)
        elif change=='version':
            with postgres.begin() as db:db.execute(text('UPDATE users SET auth_version=auth_version+1 WHERE id=1'))
        elif change=='mcp':
            with postgres.begin() as db:db.execute(text('UPDATE mcp_tokens SET revoked_at=now() WHERE user_id=1'))
    app=FastAPI();app.include_router(router.router)
    app.dependency_overrides[get_db]=lambda:None
    app.dependency_overrides[get_session_factory]=lambda:factory
    first_sent,entered,closed=asyncio.Event(),asyncio.Event(),asyncio.Event()
    aborted=[]
    async def observed_app(scope,receive,send):
        async def observe(message):
            await send(message)
            if message['type']=='http.response.body' and b'allowed' in message.get('body',b''):
                first_sent.set()
        await app(scope,receive,observe)
    class Engine:
        async def stream(self,controller,**kwargs):
            async def abort():aborted.append(True)
            controller.set_abort_callback(abort)
            try:
                yield AssistantEvent('token',{'content':'allowed'})
                await first_sent.wait()
                entered.set()
                if quiet:await asyncio.Event().wait()
                else:
                    revoke()
                    yield AssistantEvent('token',{'content':'private-late-data'})
            finally:closed.set()
    monkeypatch.setattr(service,'FastEngine',Engine)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(observed_app),base_url='http://test') as client:
        pending=asyncio.create_task(client.post(f'/assistant/conversations/{cid}/messages/stream',headers={'Authorization':'Bearer '+raw,'X-Workspace-Id':'1'},json={'query':'question'}))
        if quiet:
            await asyncio.wait_for(entered.wait(),3)
            revoke()
        response=await asyncio.wait_for(pending,8)
    assert response.status_code==200
    assert 'allowed' in response.text and 'private-late-data' not in response.text and raw not in response.text
    payloads=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
    assert payloads[-1]['type']=='cancelled'
    assert closed.is_set() and aborted
    run_id=int(response.headers['X-Assistant-Run-Id'])
    assert service.run_registry.get(run_id) is None
    with postgres.connect() as db:
        assert db.scalar(text('SELECT count(*) FROM workspace_members WHERE user_id=1'))==1
        assert db.scalar(text('SELECT status FROM assistant_runs WHERE id=:id'),{'id':run_id})=='cancelled'
        assert db.execute(text("SELECT status,content FROM assistant_messages WHERE role='assistant'")).one()==('cancelled','allowed')


@pytest.mark.parametrize('cleanup',[None,'self-cancel','repeat-cancel'])
async def test_ui_header_send_failure_closes_real_upstream_http(postgres,infrastructure,monkeypatch,cleanup):
    import threading
    from urllib.parse import urlsplit,parse_qs
    import httpx
    from fastapi import FastAPI
    from fastapi.responses import StreamingResponse
    from starlette.requests import ClientDisconnect
    from app.features.workspace import ui_gateway,ui_tickets
    from app.infra.extensions import get_session_factory
    from tests.integration.test_expert_gateway import serve
    upgrade(postgres)
    with postgres.begin() as db:
        db.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (1,'reader','x','common')"))
        db.execute(text("INSERT INTO workspaces(id,name,owner_id) VALUES (1,'one',1)"))
        db.execute(text("INSERT INTO workspace_members(workspace_id,user_id,role) VALUES (1,1,'editor')"))
        db.execute(text("INSERT INTO opencode_runtimes(id,workspace_id,user_id,status,container_id) VALUES (1,1,1,'running',:id)"),{'id':'a'*64})
    factory=sessionmaker(bind=postgres)
    monkeypatch.setattr(ui_tickets,'redis_client',infrastructure['redis'])
    monkeypatch.setenv('APP_ORIGIN','http://app.skycloud.localhost')
    monkeypatch.setenv('RUNTIME_UI_ORIGIN_TEMPLATE','http://r{runtime_id}.skycloud.localhost:8081')
    with factory() as db:
        issued=ui_tickets.issue(db,1,1)
        parsed=urlsplit(issued['launch_url'])
        raw,_=ui_tickets.redeem(db,parse_qs(parsed.query)['ticket'][0],parsed.netloc)
    upstream_closed=threading.Event()
    controller=FastAPI()
    @controller.get('/runtimes/{container_id}/ui/event')
    async def event(container_id):
        assert container_id=='a'*64
        async def chunks():
            try:
                yield b'data: upstream\n\n'
                await asyncio.Event().wait()
            finally:upstream_closed.set()
        return StreamingResponse(chunks(),media_type='text/event-stream')
    app=FastAPI();app.include_router(ui_gateway.router)
    app.dependency_overrides[get_session_factory]=lambda:factory
    real_client=httpx.AsyncClient
    clients=[]
    def tracked_client(**kwargs):
        client=real_client(**kwargs)
        clients.append(client)
        return client
    monkeypatch.setattr(ui_gateway.httpx,'AsyncClient',tracked_client)
    closing,release=asyncio.Event(),asyncio.Event()
    if cleanup:
        actual_response=ui_gateway.UpstreamResponse
        class WrappedResponse(actual_response):
            def __init__(self,*args,**kwargs):
                super().__init__(*args,**kwargs)
                iterator=self.body_iterator
                class Body:
                    def __aiter__(self):return self
                    async def __anext__(self):return await anext(iterator)
                    async def aclose(self):
                        closing.set()
                        if cleanup=='repeat-cancel':await release.wait()
                        await iterator.aclose()
                        if cleanup=='self-cancel':raise asyncio.CancelledError('private-upstream-close')
                self.body_iterator=Body()
        monkeypatch.setattr(ui_gateway,'UpstreamResponse',WrappedResponse)
    async def fail_headers(scope,receive,send):
        scope={**scope,'asgi':{'version':'3.0','spec_version':'2.4'}}
        async def failed_send(message):raise OSError('header send failed')
        await app(scope,receive,failed_send)
    with serve(controller) as port:
        monkeypatch.setattr(ui_gateway,'controller_url',lambda:f'http://127.0.0.1:{port}')
        async with real_client(transport=httpx.ASGITransport(fail_headers),base_url='http://test') as client:
            pending=asyncio.create_task(client.get('/runtime-ui/event',headers={'Host':parsed.netloc,'Cookie':ui_tickets.COOKIE+'='+raw}))
            try:
                if cleanup:
                    await asyncio.wait_for(closing.wait(),3)
                if cleanup=='repeat-cancel':
                    pending.cancel();await asyncio.sleep(0)
                    pending.cancel();await asyncio.sleep(0.01)
                    assert not pending.done() and len(clients)==1 and not clients[0].is_closed
                    assert not upstream_closed.is_set()
            finally:
                release.set()
                with pytest.raises(ClientDisconnect):await asyncio.wait_for(pending,3)
        assert len(clients)==1 and clients[0].is_closed
        assert await asyncio.to_thread(upstream_closed.wait,3)


@pytest.mark.parametrize('change,quiet,streaming', [('demote',True,True),('remove',True,True),('stop',True,True),('revoke',True,True),('delete-user',True,True),('demote',False,True),('demote',False,False)])
async def test_model_stream_current_runtime_revocation(postgres,monkeypatch,change,quiet,streaming):
    import httpx
    from fastapi import FastAPI
    from app.features.runtime_llm import router,tokens
    from app.infra.extensions import get_db,get_session_factory
    from app.models.opencode_runtime import OpenCodeRuntime
    upgrade(postgres)
    with postgres.begin() as db:
        db.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (1,'reader','x','common')"))
        db.execute(text("INSERT INTO workspaces(id,name,owner_id) VALUES (1,'one',1)"))
        db.execute(text("INSERT INTO workspace_members(workspace_id,user_id,role) VALUES (1,1,'editor')"))
    factory=sessionmaker(bind=postgres)
    with factory() as db:
        runtime=OpenCodeRuntime(workspace_id=1,user_id=1,status='running')
        db.add(runtime);db.commit()
        raw=tokens.issue_token(db,runtime)
        from app.infra.llm.config import get_chat_model_config
        model=get_chat_model_config()['model']
    entered,closed,first_sent=asyncio.Event(),asyncio.Event(),asyncio.Event()
    def revoke():
        sql={'demote':"UPDATE workspace_members SET role='viewer' WHERE user_id=1",
             'remove':'DELETE FROM workspace_members WHERE user_id=1',
             'stop':"UPDATE opencode_runtimes SET status='stopped' WHERE user_id=1",
             'revoke':'UPDATE mcp_runtime_tokens SET revoked_at=now() WHERE user_id=1',
             'delete-user':'DELETE FROM users WHERE id=1'}[change]
        with postgres.begin() as db:db.execute(text(sql))
    class Bytes(httpx.AsyncByteStream):
        async def __aiter__(self):
            try:
                yield b'data: allowed\n\n'
                if streaming:await first_sent.wait()
                entered.set()
                if quiet:await asyncio.Event().wait()
                else:
                    revoke();yield b'data: private-late\n\n'
            finally:closed.set()
        async def aclose(self):closed.set()
    actual_client=httpx.AsyncClient
    upstreams=[]
    def provider():
        client=actual_client(transport=httpx.MockTransport(lambda _:httpx.Response(200,stream=Bytes())))
        upstreams.append(client);return client
    monkeypatch.setattr(router,'upstream_client',provider)
    monkeypatch.setattr(router.quota,'reserve',lambda *args:None)
    monkeypatch.setattr(router,'get_chat_model_config',lambda:{'api':'http://synthetic/v1','key':'synthetic-server-key','model':model})
    app=FastAPI();app.include_router(router.router)
    app.dependency_overrides[get_db]=lambda:None
    app.dependency_overrides[get_session_factory]=lambda:factory
    async def observed_app(scope,receive,send):
        async def observe(message):
            await send(message)
            if message['type']=='http.response.body' and b'allowed' in message.get('body',b''):first_sent.set()
        await app(scope,receive,observe)
    async with actual_client(transport=httpx.ASGITransport(observed_app),base_url='http://test') as client:
        pending=asyncio.create_task(client.post('/v1/chat/completions',headers={'Authorization':'Bearer '+raw},json={'model':model,'messages':[{'role':'user','content':'question'}],'stream':streaming}))
        if quiet:
            await asyncio.wait_for(entered.wait(),3);revoke()
        response=await asyncio.wait_for(pending,5)
    assert response.status_code==(200 if streaming else 401)
    assert ('allowed' in response.text) is streaming
    assert 'private-late' not in response.text and raw not in response.text
    assert closed.is_set() and upstreams[0].is_closed


@pytest.mark.parametrize('stage,end',[('body','revoke'),('headers','revoke'),('body','timeout'),('headers','timeout'),('headers','completed-revoked')])
async def test_model_admission_wait_current_permission_and_timeout(postgres,monkeypatch,stage,end):
    import httpx
    from fastapi import FastAPI
    from app.features.runtime_llm import router,tokens
    from app.infra.extensions import get_db,get_session_factory
    from app.models.opencode_runtime import OpenCodeRuntime
    from app.infra.llm.config import get_chat_model_config
    upgrade(postgres)
    with postgres.begin() as db:
        db.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (1,'reader','x','common')"))
        db.execute(text("INSERT INTO workspaces(id,name,owner_id) VALUES (1,'one',1)"))
        db.execute(text("INSERT INTO workspace_members(workspace_id,user_id,role) VALUES (1,1,'editor')"))
    factory=sessionmaker(bind=postgres)
    with factory() as db:
        runtime=OpenCodeRuntime(workspace_id=1,user_id=1,status='running')
        db.add(runtime);db.commit();raw=tokens.issue_token(db,runtime)
    model=get_chat_model_config()['model']
    entered,closed=asyncio.Event(),asyncio.Event()
    charges,clients,responses=[],[],[]
    def revoke():
        with postgres.begin() as db:db.execute(text("UPDATE workspace_members SET role='viewer' WHERE user_id=1"))
    body=json.dumps({'model':model,'messages':[{'role':'user','content':'question'}],'stream':True}).encode()
    class Upload(httpx.AsyncByteStream):
        async def __aiter__(self):
            try:
                yield body[:5];entered.set();await asyncio.Event().wait();yield body[5:]
            finally:closed.set()
    actual_client=httpx.AsyncClient
    async def provider(request):
        try:
            entered.set()
            if end!='completed-revoked':await asyncio.Event().wait()
            revoke()
            response=httpx.Response(200,stream=httpx.ByteStream(b'data: private-late\n\n'))
            assert not response.is_closed
            responses.append(response);return response
        finally:closed.set()
    def upstream():
        client=actual_client(transport=httpx.MockTransport(provider));clients.append(client);return client
    monkeypatch.setattr(router,'upstream_client',upstream)
    monkeypatch.setattr(router.quota,'reserve',lambda *args:charges.append(True))
    monkeypatch.setattr(router,'get_chat_model_config',lambda:{'api':'http://synthetic/v1','key':'synthetic-server-key','model':model})
    if end=='timeout':monkeypatch.setattr(router,'MAX_BODY_SECONDS' if stage=='body' else 'MAX_HEADER_SECONDS',0.05)
    app=FastAPI();app.include_router(router.router)
    app.dependency_overrides[get_db]=lambda:None
    app.dependency_overrides[get_session_factory]=lambda:factory
    async with actual_client(transport=httpx.ASGITransport(app),base_url='http://test') as client:
        pending=asyncio.create_task(client.post('/v1/chat/completions',headers={'Authorization':'Bearer '+raw},content=Upload() if stage=='body' else body))
        await asyncio.wait_for(entered.wait(),3)
        if end=='revoke':revoke()
        response=await asyncio.wait_for(pending,5)
    expected=(408 if stage=='body' else 504) if end=='timeout' else 401
    assert response.status_code==expected
    assert raw not in response.text and 'private-late' not in response.text and closed.is_set()
    assert len(charges)==(stage=='headers') and len(clients)==(stage=='headers')
    assert all(client.is_closed for client in clients)
    assert all(response.is_closed for response in responses)


@pytest.mark.parametrize('stage',['body','headers','buffered'])
async def test_model_native_client_disconnect_releases_wait(postgres,monkeypatch,stage):
    import threading
    import httpx
    from fastapi import FastAPI
    from app.features.runtime_llm import router,tokens
    from app.infra.extensions import get_session_factory
    from app.models.opencode_runtime import OpenCodeRuntime
    from app.infra.llm.config import get_chat_model_config
    from tests.integration.test_expert_gateway import serve
    upgrade(postgres)
    with postgres.begin() as db:
        db.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (1,'reader','x','common')"))
        db.execute(text("INSERT INTO workspaces(id,name,owner_id) VALUES (1,'one',1)"))
        db.execute(text("INSERT INTO workspace_members(workspace_id,user_id,role) VALUES (1,1,'editor')"))
    factory=sessionmaker(bind=postgres)
    with factory() as db:
        runtime=OpenCodeRuntime(workspace_id=1,user_id=1,status='running')
        db.add(runtime);db.commit();raw=tokens.issue_token(db,runtime)
    model=get_chat_model_config()['model']
    received,provider_ready,provider_closed,finished=(threading.Event() for _ in range(4))
    clients,charges,errors=[],[],[]
    async def provider(scope,receive,send):
        try:
            while True:
                message=await receive()
                if not message.get('more_body'):break
            if stage=='buffered':
                await send({'type':'http.response.start','status':200,'headers':[(b'content-type',b'application/json')]})
                await send({'type':'http.response.body','body':b'{"partial":','more_body':True})
            provider_ready.set()
            while (await receive())['type']!='http.disconnect':pass
        finally:provider_closed.set()
    app=FastAPI();app.include_router(router.router)
    app.dependency_overrides[get_session_factory]=lambda:factory
    async def observed(scope,receive,send):
        async def read():
            message=await receive()
            if message['type']=='http.request':received.set()
            return message
        try:await app(scope,read,send)
        except BaseException as exc:
            errors.append(type(exc).__name__);raise
        finally:finished.set()
    def upstream():
        client=httpx.AsyncClient(timeout=10,trust_env=False);clients.append(client);return client
    monkeypatch.setattr(router,'upstream_client',upstream)
    monkeypatch.setattr(router.quota,'reserve',lambda *args:charges.append(True))
    # A failure must also leave these disposable servers able to shut down.
    monkeypatch.setattr(router,'MAX_HEADER_SECONDS',5)
    monkeypatch.setattr(router,'MAX_STREAM_SECONDS',5)
    body=json.dumps({'model':model,'messages':[{'role':'user','content':'question'}],'stream':False}).encode()
    with serve(provider) as provider_port,serve(observed) as gateway_port:
        monkeypatch.setattr(router,'get_chat_model_config',lambda:{'api':f'http://127.0.0.1:{provider_port}/v1','key':'synthetic-server-key','model':model})
        reader,writer=await asyncio.open_connection('127.0.0.1',gateway_port)
        try:
            headers=f'POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {raw}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n'.encode()
            writer.write(headers+(body[:5] if stage=='body' else body));await writer.drain()
            signal=received if stage=='body' else provider_ready
            assert await asyncio.to_thread(signal.wait,3)
        finally:
            writer.close();await writer.wait_closed()
        assert await asyncio.to_thread(finished.wait,2.5),'gateway kept processing disconnected client'
        if stage!='body':assert await asyncio.to_thread(provider_closed.wait,2.5)
    assert not errors
    assert len(charges)==(stage!='body') and len(clients)==(stage!='body')
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize('stage,end',[('body','revoke'),('headers','revoke'),('body','timeout'),('headers','timeout'),('headers','completed-revoked')])
async def test_ui_admission_current_permission_and_timeout(postgres,infrastructure,monkeypatch,stage,end):
    from urllib.parse import urlsplit,parse_qs
    import httpx
    from fastapi import FastAPI
    from app.features.workspace import ui_gateway,ui_tickets
    from app.infra.extensions import get_session_factory
    upgrade(postgres)
    with postgres.begin() as db:
        db.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (1,'reader','x','common')"))
        db.execute(text("INSERT INTO workspaces(id,name,owner_id) VALUES (1,'one',1)"))
        db.execute(text("INSERT INTO workspace_members(workspace_id,user_id,role) VALUES (1,1,'editor')"))
        db.execute(text("INSERT INTO opencode_runtimes(id,workspace_id,user_id,status,container_id) VALUES (1,1,1,'running',:id)"),{'id':'a'*64})
    factory=sessionmaker(bind=postgres)
    monkeypatch.setattr(ui_tickets,'redis_client',infrastructure['redis'])
    monkeypatch.setenv('APP_ORIGIN','http://app.skycloud.localhost')
    monkeypatch.setenv('RUNTIME_UI_ORIGIN_TEMPLATE','http://r{runtime_id}.skycloud.localhost:8081')
    with factory() as db:
        issued=ui_tickets.issue(db,1,1)
        parsed=urlsplit(issued['launch_url'])
        raw,_=ui_tickets.redeem(db,parse_qs(parsed.query)['ticket'][0],parsed.netloc)
    entered,closed=asyncio.Event(),asyncio.Event()
    clients,responses=[],[]
    def revoke():
        with postgres.begin() as db:db.execute(text("UPDATE workspace_members SET role='viewer' WHERE user_id=1"))
    class Upload(httpx.AsyncByteStream):
        async def __aiter__(self):
            try:
                yield b'partial';entered.set();await asyncio.Event().wait();yield b'private-late'
            finally:closed.set()
    real_client=httpx.AsyncClient
    async def provider(request):
        try:
            entered.set()
            if end!='completed-revoked':await asyncio.Event().wait()
            revoke()
            response=httpx.Response(200,stream=httpx.ByteStream(b'private-late'))
            assert not response.is_closed
            responses.append(response);return response
        finally:closed.set()
    def upstream(**kwargs):
        client=real_client(transport=httpx.MockTransport(provider),**kwargs);clients.append(client);return client
    monkeypatch.setattr(ui_gateway.httpx,'AsyncClient',upstream)
    if end=='timeout':monkeypatch.setattr(ui_gateway,'MAX_BODY_SECONDS' if stage=='body' else 'MAX_HEADER_SECONDS',0.05)
    app=FastAPI();app.include_router(ui_gateway.router)
    app.dependency_overrides[get_session_factory]=lambda:factory
    origin=f'{parsed.scheme}://{parsed.netloc}'
    async with real_client(transport=httpx.ASGITransport(app),base_url=origin) as client:
        pending=asyncio.create_task(client.post('/runtime-ui/event',headers={'Cookie':ui_tickets.COOKIE+'='+raw,'Origin':origin},content=Upload() if stage=='body' else b'question'))
        await asyncio.wait_for(entered.wait(),3)
        if end=='revoke':revoke()
        response=await asyncio.wait_for(pending,5)
    expected=(408 if stage=='body' else 504) if end=='timeout' else 403
    assert response.status_code==expected and closed.is_set()
    assert raw not in response.text and 'private-late' not in response.text
    assert len(clients)==(stage=='headers') and all(client.is_closed for client in clients)
    assert all(response.is_closed for response in responses)


@pytest.mark.parametrize('change,quiet',[('membership',False),('membership',True),('version',True),('family',True),('stop',True),('expiry',True)])
async def test_ui_response_current_identity_after_forwarded_chunk(postgres,infrastructure,monkeypatch,change,quiet):
    import time
    import httpx
    import jwt
    from urllib.parse import urlsplit,parse_qs
    from fastapi import FastAPI
    from app.features.workspace import ui_gateway,ui_tickets
    from app.features.auth import refresh_sessions
    from app.features.auth.service import generate_token
    from app.infra.extensions import get_session_factory,SECRET_KEY
    upgrade(postgres)
    with postgres.begin() as db:
        db.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (1,'reader','x','common')"))
        db.execute(text("INSERT INTO workspaces(id,name,owner_id) VALUES (1,'one',1)"))
        db.execute(text("INSERT INTO workspace_members(workspace_id,user_id,role) VALUES (1,1,'editor')"))
        db.execute(text("INSERT INTO opencode_runtimes(id,workspace_id,user_id,status,container_id) VALUES (1,1,1,'running',:id)"),{'id':'a'*64})
    factory=sessionmaker(bind=postgres)
    for module in (ui_tickets,refresh_sessions):monkeypatch.setattr(module,'redis_client',infrastructure['redis'])
    monkeypatch.setenv('APP_ORIGIN','http://app.skycloud.localhost')
    monkeypatch.setenv('RUNTIME_UI_ORIGIN_TEMPLATE','http://r{runtime_id}.skycloud.localhost:8081')
    parent=None
    with factory() as db:
        if change=='family':
            _,access=refresh_sessions.create(db,jwt.decode(generate_token(1,0),SECRET_KEY,algorithms=['HS256']))
            parent=jwt.decode(access['token'],SECRET_KEY,algorithms=['HS256'])['sid']
        issued=ui_tickets.issue(db,1,1,parent_session=parent)
        parsed=urlsplit(issued['launch_url'])
        raw,_=ui_tickets.redeem(db,parse_qs(parsed.query)['ticket'][0],parsed.netloc)
    if change=='expiry':
        key=ui_tickets._key('session',raw)
        payload=json.loads(infrastructure['redis'].get(key));payload['expires_at']=time.time()+3
        infrastructure['redis'].set(key,json.dumps(payload),ex=60)
    def revoke():
        if change=='family':refresh_sessions.revoke_access('Bearer '+access['token'])
        elif change!='expiry':
            statements={'membership':"UPDATE workspace_members SET role='viewer' WHERE user_id=1",
                        'version':'UPDATE users SET auth_version=auth_version+1 WHERE id=1',
                        'stop':"UPDATE opencode_runtimes SET status='stopped' WHERE id=1"}
            with postgres.begin() as db:db.execute(text(statements[change]))
    entered,closed=asyncio.Event(),asyncio.Event()
    class Stream(httpx.AsyncByteStream):
        def __init__(self):self.iterator=self.events()
        def __aiter__(self):return self.iterator
        async def events(self):
            try:
                yield b'data: allowed\n\n'
                if quiet:await asyncio.Event().wait()
                else:revoke()
                yield b'data: private-late\n\n'
            finally:closed.set()
        async def aclose(self):await self.iterator.aclose()
    real_client=httpx.AsyncClient
    clients=[]
    def upstream(**kwargs):
        client=real_client(transport=httpx.MockTransport(lambda request:httpx.Response(200,stream=Stream())),**kwargs)
        clients.append(client);return client
    monkeypatch.setattr(ui_gateway.httpx,'AsyncClient',upstream)
    app=FastAPI();app.include_router(ui_gateway.router)
    app.dependency_overrides[get_session_factory]=lambda:factory
    async def observed(scope,receive,send):
        async def forwarded(message):
            await send(message)
            if message['type']=='http.response.body' and b'allowed' in message.get('body',b''):entered.set()
        await app(scope,receive,forwarded)
    async with real_client(transport=httpx.ASGITransport(observed),base_url=f'{parsed.scheme}://{parsed.netloc}') as client:
        pending=asyncio.create_task(client.get('/runtime-ui/event',headers={'Cookie':ui_tickets.COOKIE+'='+raw}))
        await asyncio.wait_for(entered.wait(),3)
        if quiet:revoke()
        response=await asyncio.wait_for(pending,5)
    assert response.status_code==200 and 'allowed' in response.text
    assert 'private-late' not in response.text and raw not in response.text
    assert closed.is_set() and len(clients)==1 and clients[0].is_closed


@pytest.mark.parametrize('kind',['model','ui'])
async def test_http_completed_header_teardown_cancel_closes_native_upstream(postgres,infrastructure,monkeypatch,kind):
    import threading
    import httpx
    from fastapi import FastAPI
    from urllib.parse import urlsplit,parse_qs
    from app.infra.http_wait import guarded_wait
    from app.infra.extensions import get_session_factory
    from app.features.runtime_llm import router as model,tokens
    from app.features.workspace import ui_gateway,ui_tickets
    from app.infra.llm.config import get_chat_model_config
    from app.models.opencode_runtime import OpenCodeRuntime
    from tests.integration.test_expert_gateway import serve
    upgrade(postgres)
    with postgres.begin() as db:
        db.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (1,'reader','x','common')"))
        db.execute(text("INSERT INTO workspaces(id,name,owner_id) VALUES (1,'one',1)"))
        db.execute(text("INSERT INTO workspace_members(workspace_id,user_id,role) VALUES (1,1,'editor')"))
        db.execute(text("INSERT INTO opencode_runtimes(id,workspace_id,user_id,status,container_id) VALUES (1,1,1,'running',:id)"),{'id':'a'*64})
    factory=sessionmaker(bind=postgres)
    if kind=='model':
        with factory() as db:raw=tokens.issue_token(db,db.get(OpenCodeRuntime,1))
        headers={'Authorization':'Bearer '+raw};base='http://test';path='/v1/chat/completions'
        model_name=get_chat_model_config()['model']
        body=json.dumps({'model':model_name,'messages':[{'role':'user','content':'question'}],'stream':True}).encode()
        monkeypatch.setattr(model.quota,'reserve',lambda *args:None)
        gateway=model
    else:
        monkeypatch.setattr(ui_tickets,'redis_client',infrastructure['redis'])
        monkeypatch.setenv('APP_ORIGIN','http://app.skycloud.localhost')
        monkeypatch.setenv('RUNTIME_UI_ORIGIN_TEMPLATE','http://r{runtime_id}.skycloud.localhost:8081')
        with factory() as db:
            issued=ui_tickets.issue(db,1,1);parsed=urlsplit(issued['launch_url'])
            raw,_=ui_tickets.redeem(db,parse_qs(parsed.query)['ticket'][0],parsed.netloc)
        base=f'{parsed.scheme}://{parsed.netloc}';headers={'Cookie':ui_tickets.COOKIE+'='+raw,'Origin':base}
        path='/runtime-ui/event';body=b'question';gateway=ui_gateway
    clients,responses,forwarded=[],[],[]
    upstream_closed=threading.Event()
    async def provider(scope,receive,send):
        try:
            while (await receive()).get('more_body'):pass
            await send({'type':'http.response.start','status':200,'headers':[(b'content-type',b'text/event-stream')]})
            await send({'type':'http.response.body','body':b'data: private-late\n\n','more_body':True})
            while (await receive())['type']!='http.disconnect':pass
        finally:upstream_closed.set()
    real_client=httpx.AsyncClient
    def upstream(**kwargs):
        client=real_client(**kwargs);clients.append(client)
        send=client.send
        async def opened(*args,**kw):
            response=await send(*args,**kw);responses.append(response)
            assert not response.is_closed
            return response
        client.send=opened
        return client
    async def checked_wait(operation,*,check,**options):
        async def checked():
            await check()
            if responses:asyncio.get_running_loop().call_soon(asyncio.current_task().cancel)
        return await guarded_wait(operation,check=checked,**options)
    monkeypatch.setattr(gateway,'guarded_wait',checked_wait)
    if kind=='model':monkeypatch.setattr(gateway,'upstream_client',lambda:upstream(timeout=60,trust_env=False))
    else:monkeypatch.setattr(gateway.httpx,'AsyncClient',upstream)
    app=FastAPI();app.include_router(gateway.router)
    app.dependency_overrides[get_session_factory]=lambda:factory
    async def observed(scope,receive,send):
        async def sent(message):
            forwarded.append(message['type']);await send(message)
        await app(scope,receive,sent)
    with serve(provider) as port:
        if kind=='model':monkeypatch.setattr(model,'get_chat_model_config',lambda:{'api':f'http://127.0.0.1:{port}/v1','key':'synthetic-provider-key','model':model_name})
        else:monkeypatch.setattr(ui_gateway,'controller_url',lambda:f'http://127.0.0.1:{port}')
        async with real_client(transport=httpx.ASGITransport(observed),base_url=base) as client:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(client.post(path,headers=headers,content=body),3)
        assert await asyncio.to_thread(upstream_closed.wait,3)
    assert len(clients)==len(responses)==1
    assert clients[0].is_closed and responses[0].is_closed and forwarded==[]


@pytest.mark.parametrize('stage,end',[('body','disconnect'),('headers','disconnect'),('response','disconnect'),('response','membership-ready'),('response','membership'),('response','version'),('response','family'),('response','stop'),('response','expiry')])
async def test_ui_native_chain_current_identity_and_disconnect(postgres,infrastructure,monkeypatch,stage,end):
    """Canonical SQL/Redis credentials over native UI, Controller and provider sockets."""
    import threading
    import time
    import jwt
    import httpx
    from urllib.parse import urlsplit,parse_qs
    from fastapi import FastAPI
    from app.features.workspace import ui_gateway,ui_tickets
    from app.features.auth import refresh_sessions
    from app.features.auth.service import generate_token
    from app.infra.extensions import get_session_factory,SECRET_KEY
    from runtime_controller import app as controller
    from runtime_controller.policy import Settings
    from tests.integration.test_expert_gateway import serve
    upgrade(postgres)
    with postgres.begin() as db:
        db.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (1,'reader','x','common')"))
        db.execute(text("INSERT INTO workspaces(id,name,owner_id) VALUES (1,'one',1)"))
        db.execute(text("INSERT INTO workspace_members(workspace_id,user_id,role) VALUES (1,1,'editor')"))
        db.execute(text("INSERT INTO opencode_runtimes(id,workspace_id,user_id,status,container_id) VALUES (1,1,1,'running',:id)"),{'id':'a'*64})
    factory=sessionmaker(bind=postgres)
    for module in (ui_tickets,refresh_sessions):monkeypatch.setattr(module,'redis_client',infrastructure['redis'])
    monkeypatch.setenv('RUNTIME_CONTROLLER_TOKEN','T'*48)
    monkeypatch.setenv('APP_ORIGIN','http://app.skycloud.localhost')
    monkeypatch.setenv('RUNTIME_UI_ORIGIN_TEMPLATE','http://r{runtime_id}.skycloud.localhost:8081')
    parent=None
    with factory() as db:
        if end=='family':
            _,access=refresh_sessions.create(db,jwt.decode(generate_token(1,0),SECRET_KEY,algorithms=['HS256']))
            parent=jwt.decode(access['token'],SECRET_KEY,algorithms=['HS256'])['sid']
        issued=ui_tickets.issue(db,1,1,parent_session=parent);parsed=urlsplit(issued['launch_url'])
        raw,_=ui_tickets.redeem(db,parse_qs(parsed.query)['ticket'][0],parsed.netloc)
    if end=='expiry':
        key=ui_tickets._key('session',raw)
        payload=json.loads(infrastructure['redis'].get(key));payload['expires_at']=time.time()+3
        infrastructure['redis'].set(key,json.dumps(payload),ex=60)
    late_ready=threading.Event()
    received,provider_ready,provider_closed,finished=(threading.Event() for _ in range(4))
    clients,responses,errors,forwarded=[],[],[],[]
    async def provider(scope,receive,send):
        try:
            while True:
                message=await receive()
                if not message.get('more_body'):break
            if stage=='response':
                await send({'type':'http.response.start','status':200,'headers':[(b'content-type',b'text/event-stream')]})
                await send({'type':'http.response.body','body':b'data: allowed\n\n','more_body':True})
            provider_ready.set()
            if end=='membership-ready':
                while not late_ready.is_set():await asyncio.sleep(.005)
                await send({'type':'http.response.body','body':b'data: private-late\n\n','more_body':True})
            while (await receive())['type']!='http.disconnect':pass
        finally:provider_closed.set()
    app=FastAPI();app.include_router(ui_gateway.router)
    app.dependency_overrides[get_session_factory]=lambda:factory
    async def observed(scope,receive,send):
        async def read():
            message=await receive()
            if message['type']=='http.request':received.set()
            return message
        async def sent(message):
            if message['type']=='http.response.body':forwarded.append(message.get('body',b''))
            await send(message)
        try:await app(scope,read,sent)
        except BaseException as exc:errors.append(type(exc).__name__);raise
        finally:finished.set()
    real_client=httpx.AsyncClient
    def upstream(**kwargs):
        client=real_client(**kwargs);clients.append(client)
        actual_send=client.send
        async def opened(*args,**kw):
            response=await actual_send(*args,**kw)
            assert not response.is_closed
            responses.append(response);return response
        client.send=opened
        return client
    monkeypatch.setattr(ui_gateway.httpx,'AsyncClient',upstream)
    # Failure-only bounds allow isolated servers to shut down even if the
    # disconnect assertion fails; the production one-second checks remain.
    monkeypatch.setattr(ui_gateway,'MAX_HEADER_SECONDS',5)
    body=b'question'
    async def exercise(controller_port,gateway_port):
        monkeypatch.setattr(ui_gateway,'controller_url',lambda:f'http://127.0.0.1:{controller_port}')
        reader,writer=await asyncio.open_connection('127.0.0.1',gateway_port)
        try:
            headers=(f'POST /runtime-ui/event HTTP/1.1\r\nHost: {parsed.netloc}\r\nOrigin: {parsed.scheme}://{parsed.netloc}\r\nCookie: {ui_tickets.COOKIE}={raw}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n').encode()
            writer.write(headers+(body[:3] if stage=='body' else body));await writer.drain()
            assert await asyncio.to_thread((received if stage=='body' else provider_ready).wait,3)
            if stage=='response':
                # Actual downstream TCP bytes establish that delivery started,
                # before closing the caller while the native provider is quiet.
                delivered=await asyncio.wait_for(reader.readuntil(b'data: allowed\n\n'),3)
                assert b'200 OK' in delivered
                if end!='disconnect':
                    if end=='family':refresh_sessions.revoke_access('Bearer '+access['token'])
                    elif end!='expiry':
                        statements={'membership-ready':"UPDATE workspace_members SET role='viewer' WHERE user_id=1",
                                    'membership':"UPDATE workspace_members SET role='viewer' WHERE user_id=1",
                                    'version':"UPDATE users SET auth_version=auth_version+1 WHERE id=1",
                                    'stop':"UPDATE opencode_runtimes SET status='stopped' WHERE id=1"}
                        with postgres.begin() as db:db.execute(text(statements[end]))
                    late_ready.set()
                    remaining=await asyncio.wait_for(reader.read(),5)
                    assert b'private-late' not in remaining and raw.encode() not in remaining
        finally:
            writer.close();await writer.wait_closed()
        assert await asyncio.to_thread(finished.wait,2.5),'UI gateway retained disconnected caller'
        if stage!='body':assert await asyncio.to_thread(provider_closed.wait,2.5)
    monkeypatch.setattr(controller,'MAX_HEADER_SECONDS',5)
    monkeypatch.setattr(controller,'MAX_EVENT_SECONDS',5)
    with serve(provider) as provider_port:
        class Target:
            def target(self,ref):
                assert ref=='a'*64
                return f'http://127.0.0.1:{provider_port}',('server','disposable')
        settings=Settings('T'*48,'S'*48,'test/runtime@sha256:'+'a'*64,'test','1g',100000,'test','/unused')
        with serve(controller.create_app(settings,Target())) as controller_port,serve(observed) as gateway_port:
            await exercise(controller_port,gateway_port)
    assert not errors
    assert len(clients)==2*(stage!='body') and len(responses)==2*(stage=='response')
    assert all(client.is_closed for client in clients) and all(response.is_closed for response in responses)
    if stage=='response':
        assert b'data: allowed\n\n' in b''.join(forwarded)
        assert b'private-late' not in b''.join(forwarded)
