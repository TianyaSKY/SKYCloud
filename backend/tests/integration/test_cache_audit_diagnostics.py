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
