from collections.abc import Generator
from typing import Any, cast
# pyright: reportPrivateUsage=false
"""隔离 PostgreSQL/Redis 故障保留业务结果与私有诊断信息测试."""
import os
import pytest
from loguru import logger
from redis import Redis
from sqlalchemy.orm import Session, sessionmaker
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

__all__ = ["postgres", "infrastructure"]

pytestmark = pytest.mark.skipif(os.getenv('RUN_CACHE_AUDIT_DIAGNOSTICS') != '1', reason='Opt-in actual infrastructure diagnostics')
SECRET = 'private-infra-diagnostic-sentinel'


@pytest.fixture
def diagnostics() -> Generator[list[tuple[str, Any]], None, None]:
    captured: list[tuple[str, Any]] = []
    def log_sink(message: Any) -> None:
        captured.append((str(message), message.record['exception']))
    sink = logger.add(log_sink, format='{message}')
    yield captured
    logger.remove(sink)


def safe(captured: Any, error_type: Any):
    assert any('error_type=' + error_type in message for message, _ in captured)
    assert all(SECRET not in message and exception is None for message, exception in captured)


async def test_actual_redis_acl_rejection_preserves_cache_fallback(infrastructure: Any, monkeypatch: Any, diagnostics: Any) -> None:
    admin = infrastructure['redis']
    admin.acl_setuser('diagnostic-test', enabled=True, reset=True, passwords=['+disposable-redis-test'],
                      keys=['*'], commands=['-@all', '+ping'])
    options = dict(admin.connection_pool.connection_kwargs)
    options.update(username='diagnostic-test', password='disposable-redis-test')
    client = Redis(**options)
    monkeypatch.setattr(cache, 'redis_client', client)
    try:
        assert bool(getattr(client, 'ping')())
        def cache_key(value: str) -> str:
            return value

        @cache.cacheable(cache.CacheableParams(prefix=SECRET), key=cache_key)
        async def read(value: str) -> dict[str, str]:
            return {'result': value}
        assert await read(SECRET) == {'result': SECRET}
        assert cache.evict_cache_pattern(SECRET) == 0
        safe(diagnostics, 'NoPermissionError')
        # 被拒绝的写入方未在调用方的键下静默存储数据.
        assert list(admin.scan_iter(match=SECRET + '*')) == []
    finally:
        client.close()
        admin.acl_deluser('diagnostic-test')


async def test_actual_postgres_audit_rejection_preserves_tool_and_recovers(postgres: Any, monkeypatch: Any, diagnostics: Any) -> None:
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
    active = set_mcp_request_active(active=True)
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


async def test_cancelled_cache_owns_actual_paused_redis_command(infrastructure: Any, monkeypatch: Any) -> None:
    import asyncio
    import threading
    import json
    admin = infrastructure['redis']
    key = 'owned-cache:actual'
    admin.set(key, json.dumps({'previous': True}))
    entered = threading.Event()
    class ObservedRedis(Redis):
        def execute_command(self, *args: Any, **kwargs: Any) -> Any:
            if str(args[0]).upper() == 'GET':
                entered.set()
            cmd: Any = getattr(super(), 'execute_command')
            return cmd(*args, **kwargs)
    client = ObservedRedis(**admin.connection_pool.connection_kwargs)
    monkeypatch.setattr(cache, 'redis_client', client)
    business: list[str] = []
    @cache.cacheable(cache.CacheableParams(prefix=key))
    async def work():
        business.append('accepted')
        return {'updated': True}
    # 仅暂停所属 fixture Redis 服务器, 在多次调用方取消后
    # 通过真实管理连接释放它.
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
    assert business == [] and json.loads(admin.get(key)) == {'previous': True}


@pytest.mark.parametrize('mode', ['generic', 'workspace'])
def test_actual_cache_eviction_is_batched_and_workspace_scoped(infrastructure: Any, monkeypatch: Any, mode: Any) -> None:
    from app.features.file import deletion as file_deletion
    admin = infrastructure['redis']
    prefix = 'large-cache' if mode == 'generic' else 'search:fuzzy:1'
    with admin.pipeline() as pipeline:
        for index in range(1001):
            pipeline.set(f'{prefix}:{index}', 'cached')
        for index in range(20):
            pipeline.set(f'search:fuzzy:12:{index}', 'other-workspace')
        pipeline.execute()
    batches: list[int] = []
    class ObservedStore:
        def set(self, *args: Any, **kwargs: Any):
            return admin.set(*args, **kwargs)
        def scan_iter(self, **kwargs: Any) -> Any:
            assert kwargs == {'match': prefix+':*', 'count': 128}
            yield from admin.scan_iter(**kwargs)
        def delete(self, *keys: Any):
            assert 0 < len(keys) <= 128
            batches.append(len(keys))
            return admin.delete(*keys)
    monkeypatch.setattr(cache, 'redis_client', ObservedStore())
    if mode == 'generic':
        assert cache.evict_cache_pattern(prefix) >= 1001
    else:
        file_deletion.clear_search_cache(1)
    assert not list(admin.scan_iter(match=prefix+':*'))
    assert len(batches) >= 8
    assert len(list(admin.scan_iter(match='search:fuzzy:12:*'))) == 20


def test_actual_redis_generation_fences_late_search_write(infrastructure: Any, monkeypatch: Any) -> None:
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace
    from app.features.file import deletion as file_deletion, search_service
    admin = infrastructure['redis']
    monkeypatch.setattr(cache, 'redis_client', admin)
    entered, release = threading.Event(), threading.Event()
    names = ['old-name']
    reads: list[str] = []
    def query(*args: Any):
        snapshot = names[0]
        reads.append(snapshot)
        if len(reads) == 1:
            entered.set()
            assert release.wait(3)
        return SimpleNamespace(items=[SimpleNamespace(to_dict=lambda: {'name': snapshot})], total=1)
    monkeypatch.setattr(search_service.repository, 'search_names', query)
    def mock_search_revision(*args: Any) -> int:
        return 0
    def mock_search_cache_allowed(*args: Any, **kwargs: Any) -> bool:
        return True
    monkeypatch.setattr(search_service, '_search_revision', mock_search_revision)
    monkeypatch.setattr(search_service, '_search_cache_allowed', mock_search_cache_allowed)
    with ThreadPoolExecutor(max_workers=1) as executor:
        old = executor.submit(search_service._search_files_fuzzy, cast(Any, None), 1, 'name', 1, 10)
        try:
            assert entered.wait(1)
            names[0] = 'new-name'
            file_deletion.clear_search_cache(1)
        finally:
            release.set()
        assert cast(dict[str, Any], old.result())['items'][0]['name'] == 'old-name'
    assert cast(dict[str, Any], search_service._search_files_fuzzy(cast(Any, None), 1, 'name', 1, 10))['items'][0]['name'] == 'new-name'
    assert reads == ['old-name', 'new-name']
    # 仅驱逐代系标记绝不能复活旧键.
    admin.delete('cache:generation:search:fuzzy:1')
    names[0] = 'latest-name'
    assert cast(dict[str, Any], search_service._search_files_fuzzy(cast(Any, None), 1, 'name', 1, 10))['items'][0]['name'] == 'latest-name'


@pytest.mark.parametrize("mode", ["no-cleanup", "redis-denied"])
def test_actual_sql_commit_invalidates_search_without_redis_cleanup(postgres: Any, infrastructure: Any, monkeypatch: Any, mode: Any) -> None:
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
        assert cast(dict[str, Any], search_service._search_files_fuzzy(db, workspace_id, 'name', 1, 10))['items'][0]['name'] == 'old-name'
    # 模拟在权威 SQL 提交后立即退出进程:
    # 不运行文件服务 Redis 代系轮转或物理删除.
    with factory() as db:
        f = db.get(File, file_id)
        assert f is not None
        f.name = 'new-name'
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
            from app.features.file import deletion as file_deletion
            file_deletion.clear_search_cache(workspace_id)
        with factory() as db:
            assert cast(dict[str, Any], search_service._search_files_fuzzy(db, workspace_id, 'name', 1, 10))['items'][0]['name'] == 'new-name'
    finally:
        if client is not None:
            client.close()
            infrastructure['redis'].acl_deluser('revision-reader')


def test_sql_search_revision_is_atomic_scoped_and_covers_direct_writes(postgres: Any) -> None:
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
        def revision(workspace: Any):
            return db.scalar(select(Workspace.search_cache_revision).where(Workspace.id == workspace))
        assert (revision(a), revision(b)) == (1, 0)
        assert 'search_cache_revision' not in first.to_dict()
        db.execute(text('UPDATE files SET name=:name WHERE id=:id'), {'name':'rollback-name','id':file_id})
        assert revision(a) == 2
        db.rollback()
        file_rec = db.get(File, file_id)
        assert file_rec is not None
        assert revision(a) == 1 and file_rec.name == 'trigger-name'
        db.execute(text('UPDATE files SET workspace_id=:workspace WHERE id=:id'), {'workspace':b,'id':file_id})
        db.commit()
        assert (revision(a), revision(b)) == (2, 1)
        db.execute(text('DELETE FROM files WHERE id=:id'), {'id':file_id})
        db.commit()
        assert (revision(a), revision(b)) == (2, 2)


@pytest.mark.parametrize('mode', ['dirty', 'flushed', 'direct-sql'])
def test_rolled_back_search_does_not_publish_uncommitted_cache(postgres: Any, infrastructure: Any, monkeypatch: Any, mode: Any) -> None:
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
        assert cast(dict[str, Any], search_service._search_files_fuzzy(db,workspace_id,'name',1,10))['items'][0]['name'] == 'original-name'
    before = {key:admin.get(key) for key in admin.scan_iter(match=f'search:fuzzy:{workspace_id}:*')}
    assert before, 'healthy read-only search must still populate the shared cache'
    with factory() as db:
        if mode == 'direct-sql':
            db.execute(text('UPDATE files SET name=:name WHERE id=:id'), {'name':'uncommitted-name','id':file_id})
        else:
            file_rec = db.get(File,file_id)
            assert file_rec is not None
            file_rec.name = 'uncommitted-name'
            if mode == 'flushed':
                db.flush()
        assert cast(dict[str, Any], search_service._search_files_fuzzy(db,workspace_id,'name',1,10))['items'][0]['name'] == 'uncommitted-name'
        db.rollback()
    after = {key:admin.get(key) for key in admin.scan_iter(match=f'search:fuzzy:{workspace_id}:*')}
    assert after == before, 'uncommitted search result was published to shared Redis'
    with factory() as db:
        file_rec = db.get(File,file_id)
        assert file_rec is not None
        file_rec.name = 'committed-name'
        db.commit()
    with factory() as db:
        assert cast(dict[str, Any], search_service._search_files_fuzzy(db,workspace_id,'name',1,10))['items'][0]['name'] == 'committed-name'


async def test_cached_read_cancel_drains_actual_blocked_sql(postgres: Any, infrastructure: Any, monkeypatch: Any) -> None:
    import asyncio
    import time
    from sqlalchemy import text
    from app.features.auth import user_service
    upgrade(postgres)
    factory = sessionmaker(bind=postgres)
    monkeypatch.setattr(cache, 'redis_client', infrastructure['redis'])
    with factory() as db:
        user = User(username='blocked-profile-reader', password_hash='unused', role='common')
        db.add(user); db.commit()
        user_id = user.id
    pids: list[int] = []
    closed: list[str] = []

    class TrackedSession(Session):
        def close(self) -> None:
            try:
                super().close()
            finally:
                closed.append('closed')

    tracked_sessionmaker = sessionmaker(bind=postgres, class_=TrackedSession)
    def tracked_factory() -> Session:
        db = tracked_sessionmaker()
        pids.append(int(db.scalar(text('SELECT pg_backend_pid()'))))
        return db

    with postgres.connect() as locker:
        transaction = locker.begin()
        locker.execute(text('LOCK TABLE users IN ACCESS EXCLUSIVE MODE'))
        pending = asyncio.create_task(user_service.get_user(None, user_id, session_factory=tracked_factory))
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
            with pytest.raises(asyncio.CancelledError) as error:
                await asyncio.wait_for(pending,5)
        assert str(error.value) == 'caller-stop'
    assert closed == ['closed']


@pytest.mark.parametrize('kind', ['profile', 'folders', 'root-id'])
@pytest.mark.parametrize('finish', ['rollback', 'commit'])
def test_other_sql_caches_bypass_uncommitted_transactions(postgres: Any, infrastructure: Any, monkeypatch: Any, kind: Any, finish: Any) -> None:
    from app.features.auth import user_service
    from app.features.folder import service as folders
    from app.models.folder import Folder
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
        db.add_all([child]); db.commit()
        uid, wid, root_id, child_id = user.id,workspace.id,root.id,child.id
    def read(db: Any) -> Any:
        if kind == 'profile':
            user_data = user_service.get_user_data(db, uid)
            assert user_data is not None
            return user_data['username']
        if kind == 'folders':
            return next(row['name'] for row in folders.get_folders(db, wid) if row['id'] == child_id)
        return folders.get_root_folder_id(db, wid)
    expected_original = root_id if kind == 'root-id' else 'original-name'
    with factory() as db:
        assert read(db) == expected_original
    before = {key:admin.get(key) for key in admin.scan_iter()}
    assert before
    with factory() as db:
        replacement: Folder | None = None
        if kind == 'profile':
            u = db.get(User, uid)
            assert u is not None
            u.username = 'uncommitted-name'
        elif kind == 'folders':
            f = db.get(Folder, child_id)
            assert f is not None
            f.name = 'uncommitted-name'
        else:
            replacement = Folder(name='new-root', workspace_id=wid)
            db.add(replacement)
            db.flush()
            rf = db.get(Folder, root_id)
            assert rf is not None
            rf.parent_id = replacement.id
        db.flush()
        expected = replacement.id if kind == 'root-id' and replacement is not None else 'uncommitted-name'
        assert read(db) == expected
        if finish == "commit":
            db.commit()
        else:
            db.rollback()
    assert {key:admin.get(key) for key in admin.scan_iter()} == before
    with factory() as db:
        assert read(db) == (expected if finish == "commit" else expected_original)


@pytest.mark.parametrize('phase', ['pending-rollback', 'flushed-rollback', 'commit'])
def test_root_file_id_cache_tracks_committed_tree_and_bypasses_writes(postgres: Any, infrastructure: Any, monkeypatch: Any, phase: Any) -> None:
    from app.features.file import service as files
    from app.models.folder import Folder
    from app.models.file import File
    upgrade(postgres)
    factory = sessionmaker(bind=postgres)
    admin = infrastructure['redis']
    monkeypatch.setattr(cache, 'redis_client', admin)
    with factory() as db:
        user = User(username='root-file-cache-owner', password_hash='unused', role='common')
        db.add(user); db.flush()
        workspace = Workspace(name='root-file-cache', owner_id=user.id)
        db.add(workspace); db.flush()
        root = Folder(name='/', workspace_id=workspace.id)
        original = File(name='original', file_path='original-object', workspace_id=workspace.id)
        db.add_all([root, original]); db.commit()
        wid, root_id, original_id = workspace.id, root.id, original.id
    with factory() as db:
        assert files.get_root_file_id(db, wid) == original_id
    before = {key: admin.get(key) for key in admin.scan_iter()}
    assert before
    with factory() as db:
        file_rec = db.get(File, original_id)
        assert file_rec is not None
        file_rec.parent_id = root_id
        if phase != 'pending-rollback':
            db.flush()
        if phase == 'commit':
            db.commit()
        else:
            assert files.get_root_file_id(db, wid) is None
            db.rollback()
    assert {key: admin.get(key) for key in admin.scan_iter()} == before
    with factory() as db:
        assert files.get_root_file_id(db, wid) == (None if phase == 'commit' else original_id)
    # 废弃的无版本缓存条目绝不能覆盖新鲜的 SQL 读取.
    admin.set(f'workspace:root:file:{wid}', 999999)
    with factory() as db:
        assert files.get_root_file_id(db, wid) == (None if phase == 'commit' else original_id)


def test_generic_cache_revisions_are_scoped_atomic_and_survive_owner_deletion(postgres: Any) -> None:
    from sqlalchemy import text
    from app.models.folder import Folder
    from app.models.file import File
    upgrade(postgres)
    factory = sessionmaker(bind=postgres)
    with factory() as db:
        user = User(username='revision-retention', password_hash='unused',role='common')
        db.add(user); db.flush()
        first = Workspace(name='revision-a',owner_id=user.id)
        second = Workspace(name='revision-b',owner_id=user.id)
        db.add_all([first,second]); db.flush()
        root = Folder(name='/',workspace_id=first.id)
        db.add(root); db.flush()
        item = File(name='revision-item',file_path='revision-object',workspace_id=first.id,parent_id=root.id)
        db.add(item); db.commit()
        uid,a,b,file_id = user.id,first.id,second.id,item.id
        profile = f'user:profile:{uid}'
        tree_a,tree_b = f'workspace:tree:{a}',f'workspace:tree:{b}'
        def revision(scope: str) -> int:
            return cache.sql_cache_revision(db, scope)
        assert (revision(profile),revision(tree_a),revision(tree_b)) == (1,2,0)
        db.add(Folder(name='child',workspace_id=a,parent_id=root.id)); db.commit()
        assert (revision(tree_a),revision(tree_b)) == (3,0)
        db.execute(text('UPDATE files SET workspace_id=:workspace WHERE id=:id'),{'workspace':b,'id':file_id})
        assert (revision(tree_a),revision(tree_b)) == (4,1)
        db.rollback()
        assert (revision(tree_a),revision(tree_b)) == (3,0)
        db.execute(text('DELETE FROM files WHERE id=:id'),{'id':file_id}); db.commit()
        assert revision(tree_a) == 4
        db.execute(text('DELETE FROM users WHERE id=:id'),{'id':uid}); db.commit()
        assert revision(profile) == 2 and revision(tree_a) > 4
        assert revision(tree_b) == 0
