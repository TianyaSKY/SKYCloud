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
