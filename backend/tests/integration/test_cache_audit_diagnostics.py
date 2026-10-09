"""Actual isolated PostgreSQL/Redis failures preserve outcomes and private diagnostics."""
import os
import pytest
from loguru import logger
from redis import Redis
from sqlalchemy.orm import sessionmaker
from tests.integration.test_migrations import postgres
from tests.integration.test_runtime_security import infrastructure
from entry.migrate import upgrade
from app.infra import cache
from app.mcp import audit
from app.mcp.context import (McpRequestContext, set_mcp_context, reset_mcp_context,
                            set_mcp_request_active, reset_mcp_request_active)
from app.models.user import User
from app.models.workspace import Workspace
from app.models.mcp_audit_log import McpAuditLog

pytestmark = pytest.mark.skipif(os.getenv('RUN_CACHE_AUDIT_DIAGNOSTICS') != '1', reason='Opt-in actual infrastructure diagnostics')
SECRET = 'private-infra-diagnostic-sentinel'


@pytest.fixture
def diagnostics():
    captured = []
    sink = logger.add(lambda message: captured.append((str(message), message.record['exception'])), format='{message}')
    yield captured
    logger.remove(sink)


def safe(captured, error_type):
    assert any('error_type=' + error_type in message for message, _ in captured)
    assert all(SECRET not in message and exception is None for message, exception in captured)


async def test_actual_redis_acl_rejection_preserves_cache_fallback(infrastructure, monkeypatch, diagnostics):
    admin = infrastructure['redis']
    admin.acl_setuser('diagnostic-test', enabled=True, reset=True, passwords=['+disposable-redis-test'],
                      keys=['*'], commands=['-@all', '+ping'])
    options = dict(admin.connection_pool.connection_kwargs)
    options.update(username='diagnostic-test', password='disposable-redis-test')
    client = Redis(**options)
    monkeypatch.setattr(cache, 'redis_client', client)
    try:
        assert client.ping()
        @cache.cacheable(SECRET, key=lambda value: value)
        async def read(value):
            return {'result': value}
        @cache.cache_put(SECRET)
        def put(value):
            return value
        assert await read(SECRET) == {'result': SECRET}
        assert put(SECRET) == SECRET
        cache.evict_cache(SECRET, SECRET)
        assert cache.evict_cache_pattern(SECRET) == 0
        safe(diagnostics, 'NoPermissionError')
        # The denied writer did not silently store data under the caller's key.
        assert list(admin.scan_iter(match=SECRET + '*')) == []
    finally:
        client.close()
        admin.acl_deluser('diagnostic-test')


async def test_actual_postgres_audit_rejection_preserves_tool_and_recovers(postgres, monkeypatch, diagnostics):
    upgrade(postgres)
    factory = sessionmaker(bind=postgres)
    with factory() as seed:
        user = User(username='audit-owner', password_hash='isolated-test', role='common')
        seed.add(user); seed.flush()
        workspace = Workspace(name='Audit test', owner_id=user.id)
        seed.add(workspace); seed.commit()
        uid, wid = user.id, workspace.id
    monkeypatch.setattr(audit, 'SessionLocal', factory)
    context = set_mcp_context(McpRequestContext(user_id=uid, workspace_id=wid, role='viewer'))
    active = set_mcp_request_active(True)
    @audit.audited_tool(SECRET * 5)  # PostgreSQL varchar(100) rejects the real INSERT.
    async def rejected():
        return 'business result'
    @audit.audited_tool('healthy_after_failure')
    async def healthy():
        return 'healthy result'
    try:
        assert await rejected() == 'business result'
        safe(diagnostics, 'DataError')
        with factory() as observer:
            assert observer.query(McpAuditLog).count() == 0
        assert await healthy() == 'healthy result'
        with factory() as observer:
            row = observer.query(McpAuditLog).one()
            assert row.tool_name == 'healthy_after_failure' and row.success
    finally:
        reset_mcp_request_active(active)
        reset_mcp_context(context)


@pytest.mark.parametrize('operation', ['read', 'put', 'evict'])
async def test_cancelled_cache_owns_actual_paused_redis_command(infrastructure, monkeypatch, operation):
    import asyncio
    import threading
    import json
    admin = infrastructure['redis']
    key = 'owned-cache:actual'
    admin.set(key, json.dumps({'previous': True}))
    entered = threading.Event()
    expected = 'GET' if operation == 'read' else 'SETEX' if operation == 'put' else 'DEL'
    class ObservedRedis(Redis):
        def execute_command(self, *args, **kwargs):
            if str(args[0]).upper() == expected:
                entered.set()
            return super().execute_command(*args, **kwargs)
    client = ObservedRedis(**admin.connection_pool.connection_kwargs)
    monkeypatch.setattr(cache, 'redis_client', client)
    business = []
    decorator = cache.cacheable(prefix=key) if operation == 'read' else cache.cache_put(prefix=key) if operation == 'put' else cache.cache_evict(prefix=key)
    @decorator
    async def work():
        business.append('accepted')
        return {'updated': True}
    # Pause only the owned fixture Redis server, then release it through its
    # real administrative connection after repeated caller cancellation.
    admin.execute_command('CLIENT', 'PAUSE', 3000, 'ALL')
    pending = asyncio.create_task(work())
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        pending.cancel('caller-stop')
        await asyncio.sleep(.01)
        pending.cancel('repeat-stop')
        await asyncio.sleep(.01)
        assert not pending.done()
    finally:
        admin.execute_command('CLIENT', 'UNPAUSE')
        try:
            with pytest.raises(asyncio.CancelledError) as error:
                await asyncio.wait_for(pending, 2)
        finally:
            client.close()
    assert str(error.value) == 'caller-stop'
    actual = admin.get(key)
    if operation == 'read':
        assert business == [] and json.loads(actual) == {'previous': True}
    elif operation == 'put':
        assert business == ['accepted'] and json.loads(actual) == {'updated': True}
    else:
        assert business == ['accepted'] and actual is None


@pytest.mark.parametrize('mode', ['generic', 'workspace'])
def test_actual_cache_eviction_is_batched_and_workspace_scoped(infrastructure, monkeypatch, mode):
    from app.features.file import service as file_service
    admin = infrastructure['redis']
    prefix = 'large-cache' if mode == 'generic' else 'search:fuzzy:1'
    with admin.pipeline() as pipeline:
        for index in range(1001):
            pipeline.set(f'{prefix}:{index}', 'cached')
        for index in range(20):
            pipeline.set(f'search:fuzzy:12:{index}', 'other-workspace')
        pipeline.execute()
    batches = []
    class ObservedStore:
        def set(self, *args, **kwargs):
            return admin.set(*args, **kwargs)
        def scan_iter(self, **kwargs):
            assert kwargs == {'match': prefix+':*', 'count': 128}
            yield from admin.scan_iter(**kwargs)
        def delete(self, *keys):
            assert 0 < len(keys) <= 128
            batches.append(len(keys))
            return admin.delete(*keys)
    monkeypatch.setattr(cache, 'redis_client', ObservedStore())
    if mode == 'generic':
        assert cache.evict_cache_pattern(prefix) >= 1001
    else:
        file_service._clear_search_cache(1)
    assert not list(admin.scan_iter(match=prefix+':*'))
    assert len(batches) >= 8
    assert len(list(admin.scan_iter(match='search:fuzzy:12:*'))) == 20


def test_actual_redis_generation_fences_late_search_write(infrastructure, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace
    from app.features.file import service as file_service, search_service
    admin = infrastructure['redis']
    monkeypatch.setattr(cache, 'redis_client', admin)
    entered, release = threading.Event(), threading.Event()
    names = ['old-name']
    reads = []
    def query(*args):
        snapshot = names[0]
        reads.append(snapshot)
        if len(reads) == 1:
            entered.set()
            assert release.wait(3)
        return SimpleNamespace(items=[SimpleNamespace(to_dict=lambda: {'name': snapshot})], total=1)
    monkeypatch.setattr(search_service.repository, 'search_names', query)
    monkeypatch.setattr(search_service, '_search_revision', lambda *args: 0)
    monkeypatch.setattr(search_service, '_search_cache_allowed', lambda *args, **kwargs: True)
    with ThreadPoolExecutor(max_workers=1) as executor:
        old = executor.submit(search_service._search_files_fuzzy, None, 1, 'name', 1, 10)
        try:
            assert entered.wait(1)
            names[0] = 'new-name'
            file_service._clear_search_cache(1)
        finally:
            release.set()
        assert old.result()['items'][0]['name'] == 'old-name'
    assert search_service._search_files_fuzzy(None, 1, 'name', 1, 10)['items'][0]['name'] == 'new-name'
    assert reads == ['old-name', 'new-name']
    # Eviction of only the generation marker must never resurrect old keys.
    admin.delete('cache:generation:search:fuzzy:1')
    names[0] = 'latest-name'
    assert search_service._search_files_fuzzy(None, 1, 'name', 1, 10)['items'][0]['name'] == 'latest-name'


@pytest.mark.parametrize("mode", ["no-cleanup", "redis-denied"])
def test_actual_sql_commit_invalidates_search_without_redis_cleanup(postgres, infrastructure, monkeypatch, mode):
    from app.features.file import search_service
    from app.models.file import File
    upgrade(postgres)
    factory = sessionmaker(bind=postgres)
    monkeypatch.setattr(cache, 'redis_client', infrastructure['redis'])
    with factory() as db:
        user = User(username='cache-revision-owner', password_hash='unused', role='common')
        db.add(user); db.flush()
        workspace = Workspace(name='cache-revision', owner_id=user.id)
        db.add(workspace); db.flush()
        item = File(name='old-name', file_path='cache-revision-object', workspace_id=workspace.id, uploader_id=user.id)
        db.add(item); db.commit()
        workspace_id, file_id = workspace.id, item.id
        assert search_service._search_files_fuzzy(db, workspace_id, 'name', 1, 10)['items'][0]['name'] == 'old-name'
    # Simulates process exit immediately after the authoritative SQL commit:
    # no file-service Redis generation rotation or physical deletion runs.
    with factory() as db:
        db.get(File, file_id).name = 'new-name'
        db.commit()
    client = None
    try:
        if mode == 'redis-denied':
            admin = infrastructure['redis']
            admin.acl_setuser('revision-reader', enabled=True, reset=True, passwords=['+disposable-test'],
                keys=['*'], commands=['+@all', '-set', '-del'])
            options = dict(admin.connection_pool.connection_kwargs)
            options.update(username='revision-reader', password='disposable-test')
            client = Redis(**options)
            monkeypatch.setattr(cache, 'redis_client', client)
            from app.features.file import service as file_service
            file_service._clear_search_cache(workspace_id)
        with factory() as db:
            assert search_service._search_files_fuzzy(db, workspace_id, 'name', 1, 10)['items'][0]['name'] == 'new-name'
    finally:
        if client is not None:
            client.close()
            infrastructure['redis'].acl_deluser('revision-reader')


def test_sql_search_revision_is_atomic_scoped_and_covers_direct_writes(postgres):
    from app.models.file import File
    from sqlalchemy import select, text
    upgrade(postgres)
    factory = sessionmaker(bind=postgres)
    with factory() as db:
        user = User(username='revision-trigger-owner', password_hash='unused', role='common')
        db.add(user); db.flush()
        first = Workspace(name='revision-first', owner_id=user.id)
        second = Workspace(name='revision-second', owner_id=user.id)
        db.add_all([first, second]); db.flush()
        a, b = first.id, second.id
        item = File(name='trigger-name', file_path='trigger-object', workspace_id=a)
        db.add(item); db.commit()
        file_id = item.id
        def revision(workspace):
            return db.scalar(select(Workspace.search_cache_revision).where(Workspace.id == workspace))
        assert (revision(a), revision(b)) == (1, 0)
        assert 'search_cache_revision' not in first.to_dict()
        db.execute(text('UPDATE files SET name=:name WHERE id=:id'), {'name':'rollback-name','id':file_id})
        assert revision(a) == 2
        db.rollback()
        assert revision(a) == 1 and db.get(File,file_id).name == 'trigger-name'
        db.execute(text('UPDATE files SET workspace_id=:workspace WHERE id=:id'), {'workspace':b,'id':file_id})
        db.commit()
        assert (revision(a), revision(b)) == (2, 1)
        db.execute(text('DELETE FROM files WHERE id=:id'), {'id':file_id})
        db.commit()
        assert (revision(a), revision(b)) == (2, 2)


@pytest.mark.parametrize('mode', ['dirty', 'flushed', 'direct-sql'])
def test_rolled_back_search_does_not_publish_uncommitted_cache(postgres, infrastructure, monkeypatch, mode):
    from app.models.file import File
    from app.features.file import search_service
    from sqlalchemy import text
    upgrade(postgres)
    factory = sessionmaker(bind=postgres)
    admin = infrastructure['redis']
    monkeypatch.setattr(cache, 'redis_client', admin)
    with factory() as db:
        user = User(username='uncommitted-cache-owner', password_hash='unused', role='common')
        db.add(user); db.flush()
        workspace = Workspace(name='uncommitted-cache', owner_id=user.id)
        db.add(workspace); db.flush()
        item = File(name='original-name', file_path='uncommitted-object', workspace_id=workspace.id)
        db.add(item); db.commit()
        workspace_id, file_id = workspace.id, item.id
    with factory() as db:
        assert search_service._search_files_fuzzy(db,workspace_id,'name',1,10)['items'][0]['name'] == 'original-name'
    before = {key:admin.get(key) for key in admin.scan_iter(match=f'search:fuzzy:{workspace_id}:*')}
    assert before, 'healthy read-only search must still populate the shared cache'
    with factory() as db:
        if mode == 'direct-sql':
            db.execute(text('UPDATE files SET name=:name WHERE id=:id'), {'name':'uncommitted-name','id':file_id})
        else:
            db.get(File,file_id).name = 'uncommitted-name'
            if mode == 'flushed':
                db.flush()
        assert search_service._search_files_fuzzy(db,workspace_id,'name',1,10)['items'][0]['name'] == 'uncommitted-name'
        db.rollback()
    after = {key:admin.get(key) for key in admin.scan_iter(match=f'search:fuzzy:{workspace_id}:*')}
    assert after == before, 'uncommitted search result was published to shared Redis'
    with factory() as db:
        db.get(File,file_id).name = 'committed-name'
        db.commit()
    with factory() as db:
        assert search_service._search_files_fuzzy(db,workspace_id,'name',1,10)['items'][0]['name'] == 'committed-name'


@pytest.mark.parametrize('operation', ['user-owned', 'dictionary-shared'])
async def test_cached_read_cancel_drains_actual_blocked_sql(postgres, infrastructure, monkeypatch, operation):
    import asyncio
    import time
    from contextlib import contextmanager
    from sqlalchemy import text
    from app.features.auth import user_service
    from app.features.sys_dict import service as dictionary
    upgrade(postgres)
    factory = sessionmaker(bind=postgres)
    monkeypatch.setattr(cache, 'redis_client', infrastructure['redis'])
    with factory() as db:
        user = User(username='blocked-profile-reader', password_hash='unused', role='common')
        db.add(user); db.commit()
        user_id = user.id
    pids, closed = [], []
    @contextmanager
    def tracked_factory():
        with factory() as db:
            pids.append(db.scalar(text('SELECT pg_backend_pid()')))
            try:
                yield db
            finally:
                closed.append('closed')
    shared = None
    with postgres.connect() as locker:
        transaction = locker.begin()
        locker.execute(text('LOCK TABLE '+('users' if operation == 'user-owned' else 'sys_dict')+' IN ACCESS EXCLUSIVE MODE'))
        if operation == 'user-owned':
            pending = asyncio.create_task(user_service.get_user(None,user_id,session_factory=tracked_factory))
        else:
            shared = factory()
            pids.append(shared.scalar(text('SELECT pg_backend_pid()')))
            pending = asyncio.create_task(dictionary.get_sys_dict_all(shared))
        try:
            deadline = time.monotonic()+5
            while True:
                with postgres.connect() as observer:
                    waiting = observer.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE pid = ANY(:pids) AND wait_event_type='Lock')"), {'pids':pids})
                if waiting:
                    break
                assert time.monotonic()<deadline, 'owned SQL read never reached real table lock'
                await asyncio.sleep(.01)
            pending.cancel('caller-stop')
            await asyncio.sleep(.01)
            pending.cancel('repeat-stop')
            await asyncio.sleep(.01)
            assert not pending.done() and not closed
        finally:
            transaction.rollback()
            try:
                with pytest.raises(asyncio.CancelledError) as error:
                    await asyncio.wait_for(pending,5)
            finally:
                if shared is not None:
                    shared.close()
        assert str(error.value) == 'caller-stop'
    assert closed == (['closed'] if operation == 'user-owned' else [])


@pytest.mark.parametrize('kind', ['profile', 'dictionary', 'folders', 'root-id', 'root-files'])
def test_other_sql_caches_bypass_uncommitted_transactions(postgres, infrastructure, monkeypatch, kind):
    from app.features.auth import user_service
    from app.features.sys_dict import service as dictionaries
    from app.features.folder import service as folders
    from app.models.folder import Folder
    from app.models.file import File
    from app.models.sys_dict import SysDict
    upgrade(postgres)
    factory = sessionmaker(bind=postgres)
    admin = infrastructure['redis']
    monkeypatch.setattr(cache, 'redis_client', admin)
    with factory() as db:
        user = User(username='original-name', password_hash='unused', role='common')
        db.add(user); db.flush()
        workspace = Workspace(name='other-cache', owner_id=user.id)
        db.add(workspace); db.flush()
        root = Folder(name='/', workspace_id=workspace.id)
        db.add(root); db.flush()
        child = Folder(name='original-name', workspace_id=workspace.id, parent_id=root.id)
        item = File(name='original-name',file_path='other-cache-object',workspace_id=workspace.id,parent_id=root.id)
        option = SysDict(key='transaction-test-option',value='original-name',enable=True)
        db.add_all([child,item,option]); db.commit()
        uid, wid, root_id, child_id, file_id, option_id = user.id,workspace.id,root.id,child.id,item.id,option.id
    def read(db):
        if kind == 'profile': return user_service._get_user_data(db,uid)['username']
        if kind == 'dictionary': return next(row['value'] for row in dictionaries.get_sys_dict_all_sync(db) if row['key']=='transaction-test-option')
        if kind == 'folders': return next(row['name'] for row in folders.get_folders(db,wid) if row['id']==child_id)
        if kind == 'root-files': return folders.get_files_in_root_folder(db,wid)[0]['name']
        return folders.get_root_folder_id(db,wid)
    expected_original = root_id if kind == 'root-id' else 'original-name'
    with factory() as db:
        assert read(db) == expected_original
    before = {key:admin.get(key) for key in admin.scan_iter()}
    assert before
    with factory() as db:
        if kind == 'profile': db.get(User,uid).username = 'uncommitted-name'
        elif kind == 'dictionary': db.get(SysDict,option_id).value = 'uncommitted-name'
        elif kind == 'folders': db.get(Folder,child_id).name = 'uncommitted-name'
        elif kind == 'root-files': db.get(File,file_id).name = 'uncommitted-name'
        else:
            replacement = Folder(name='new-root',workspace_id=wid)
            db.add(replacement); db.flush()
            db.get(Folder,root_id).parent_id = replacement.id
        db.flush()
        expected = replacement.id if kind == 'root-id' else 'uncommitted-name'
        assert read(db) == expected
        db.rollback()
    assert {key:admin.get(key) for key in admin.scan_iter()} == before
    with factory() as db:
        assert read(db) == expected_original
