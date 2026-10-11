"""Failures retain business outcomes without diagnostic data/traceback leaks."""
from unittest.mock import MagicMock

import pytest
from loguru import logger
from sqlalchemy.exc import StatementError

from app.infra import cache
from app.exceptions import BusinessRuleError
from app.mcp import audit
from app.mcp.context import (McpRequestContext, set_mcp_context, reset_mcp_context,
                            set_mcp_request_active, reset_mcp_request_active)
from app.features.auth import service as auth

SECRET = 'private-cache-key-and-sql-bind-sentinel'


@pytest.fixture
def diagnostic_capture():
    messages = []
    sink = logger.add(lambda message: messages.append((str(message), message.record['exception'])), format='{message}')
    yield messages
    logger.remove(sink)


def assert_safe(messages):
    assert messages
    assert all(SECRET not in message and exception is None for message, exception in messages)
    assert any('error_type=' in message for message, _ in messages)


@pytest.mark.parametrize('operation', ['get', 'setex', 'delete', 'scan', 'put', 'manual', 'pattern', 'json'])
@pytest.mark.parametrize('asynchronous', [False, True])
async def test_cache_failures_preserve_fallback_and_private_keys(monkeypatch, diagnostic_capture, operation, asynchronous):
    redis = MagicMock()
    redis.get.return_value = None
    if operation == 'json':
        redis.get.return_value = SECRET
    else:
        method = {'put': 'setex', 'manual': 'delete', 'pattern': 'scan_iter', 'scan': 'scan_iter'}.get(operation, operation)
        getattr(redis, method).side_effect = RuntimeError(SECRET)
    monkeypatch.setattr(cache, 'redis_client', redis)
    if operation in {'manual', 'pattern'}:
        if operation == 'manual':
            cache.evict_cache(SECRET, SECRET)
        else:
            assert cache.evict_cache_pattern(SECRET) == 0
    else:
        decorator = cache.cache_put if operation == 'put' else cache.cache_evict if operation in {'delete', 'scan'} else cache.cacheable
        options = {'all_entries': True} if operation == 'scan' else {}
        if asynchronous:
            @decorator(prefix=SECRET, key=lambda value: value, **options)
            async def work(value):
                return {'business': value}
            assert await work(SECRET) == {'business': SECRET}
        else:
            @decorator(prefix=SECRET, key=lambda value: value, **options)
            def work(value):
                return {'business': value}
            assert work(SECRET) == {'business': SECRET}
    assert_safe(diagnostic_capture)


@pytest.mark.parametrize('failure', ['create', 'commit', 'rollback', 'close'])
@pytest.mark.parametrize('tool_fails', [False, True])
async def test_audit_failures_do_not_replace_tool_result(monkeypatch, diagnostic_capture, failure, tool_fails):
    session = MagicMock()
    error = StatementError(SECRET, 'INSERT private SQL', {'secret': SECRET}, RuntimeError(SECRET))
    factory = MagicMock(return_value=session)
    if failure == 'create':
        factory.side_effect = error
    elif failure == 'close':
        session.close.side_effect = error
    else:
        session.commit.side_effect = error
        if failure == 'rollback':
            session.rollback.side_effect = RuntimeError(SECRET)
    monkeypatch.setattr(audit, 'SessionLocal', factory)
    context = set_mcp_context(McpRequestContext(user_id=1, workspace_id=2, role='viewer'))
    active = set_mcp_request_active(True)
    original = BusinessRuleError('original tool failure')
    @audit.audited_tool('known_tool')
    async def tool():
        if tool_fails:
            raise original
        return {'answer': 'preserved'}
    try:
        if tool_fails:
            with pytest.raises(BusinessRuleError) as raised:
                await tool()
            assert raised.value is original
        else:
            assert await tool() == {'answer': 'preserved'}
    finally:
        reset_mcp_request_active(active)
        reset_mcp_context(context)
    if failure != 'create':
        session.close.assert_called_once()
        if failure in {'commit', 'rollback'}:
            session.rollback.assert_called_once()
    assert_safe(diagnostic_capture)


@pytest.mark.parametrize('mcp', [False, True])
def test_signing_failure_does_not_log_private_material(monkeypatch, diagnostic_capture, mcp):
    monkeypatch.setattr(auth.jwt, 'encode', MagicMock(side_effect=RuntimeError(SECRET)))
    assert (auth.generate_mcp_token(1) if mcp else auth.generate_token(1)) is None
    assert_safe(diagnostic_capture)


def test_registration_sql_failure_is_sanitized(monkeypatch, diagnostic_capture):
    session = MagicMock()
    session.query.return_value.filter_by.return_value.first.return_value = None
    error = StatementError(SECRET, 'INSERT users', {'password': SECRET}, RuntimeError(SECRET))
    monkeypatch.setattr(auth, 'create_user', MagicMock(side_effect=error))
    from app.exceptions import ServiceOperationError
    with pytest.raises(ServiceOperationError):
        auth.register_user(session, SECRET, SECRET)
    session.rollback.assert_called_once()
    assert_safe(diagnostic_capture)
