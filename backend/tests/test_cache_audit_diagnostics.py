"""故障保留业务结果且不泄露诊断数据或堆栈信息测试."""
from collections.abc import Generator
from typing import Any
from unittest.mock import MagicMock

import pytest
from loguru import logger
from sqlalchemy.exc import StatementError

from app.exceptions import BusinessRuleError
from app.features.auth import service as auth
from app.infra import cache
from app.mcp import audit
from app.mcp.context import (
    McpRequestContext,
    reset_mcp_context,
    reset_mcp_request_active,
    set_mcp_context,
    set_mcp_request_active,
)

SECRET = 'private-cache-key-and-sql-bind-sentinel'


@pytest.fixture
def diagnostic_capture() -> Generator[list[tuple[str, Any]], None, None]:
    messages: list[tuple[str, Any]] = []

    def sink(msg: Any) -> None:
        messages.append((str(msg), msg.record['exception']))

    sink_id = logger.add(sink, format='{message}')
    yield messages
    logger.remove(sink_id)


def assert_safe(messages: list[tuple[str, Any]]) -> None:
    assert messages
    assert all(SECRET not in message and exception is None for message, exception in messages)
    assert any('error_type=' in message for message, _ in messages)


@pytest.mark.parametrize('operation', ['get', 'setex', 'pattern', 'json'])
@pytest.mark.parametrize('asynchronous', [False, True])
async def test_cache_failures_preserve_fallback_and_private_keys(
    monkeypatch: pytest.MonkeyPatch,
    diagnostic_capture: list[tuple[str, Any]],
    operation: str,
    asynchronous: bool,
) -> None:
    redis = MagicMock()
    redis.get.return_value = None
    if operation == 'json':
        redis.get.return_value = SECRET
    elif operation == 'pattern':
        redis.scan_iter.side_effect = RuntimeError(SECRET)
    else:
        getattr(redis, operation).side_effect = RuntimeError(SECRET)
    monkeypatch.setattr(cache, 'redis_client', redis)
    if operation == 'pattern':
        assert cache.evict_cache_pattern(SECRET) == 0
    else:
        def _key(value: str) -> str:
            return value

        decorator_fn: Any = cache.cacheable(
            cache.CacheableParams(prefix=SECRET),
            key=_key,
        )
        if asynchronous:

            @decorator_fn
            async def work_async(value: str) -> dict[str, str]:
                return {'business': value}

            assert await work_async(SECRET) == {'business': SECRET}
        else:

            @decorator_fn
            def work_sync(value: str) -> dict[str, str]:
                return {'business': value}

            assert work_sync(SECRET) == {'business': SECRET}
    assert_safe(diagnostic_capture)


@pytest.mark.parametrize('failure', ['create', 'commit', 'rollback', 'close'])
@pytest.mark.parametrize('tool_fails', [False, True])
async def test_audit_failures_do_not_replace_tool_result(
    monkeypatch: pytest.MonkeyPatch,
    diagnostic_capture: list[tuple[str, Any]],
    failure: str,
    tool_fails: bool,
) -> None:
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
    active = set_mcp_request_active(active=True)
    original = BusinessRuleError('original tool failure')

    @audit.audited_tool('known_tool')
    async def tool() -> dict[str, str]:
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
def test_signing_failure_does_not_log_private_material(
    monkeypatch: pytest.MonkeyPatch,
    diagnostic_capture: list[tuple[str, Any]],
    mcp: bool,
) -> None:
    monkeypatch.setattr(auth.jwt, 'encode', MagicMock(side_effect=RuntimeError(SECRET)))
    assert (auth.generate_mcp_token(1) if mcp else auth.generate_token(1)) is None
    assert_safe(diagnostic_capture)


def test_registration_sql_failure_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    diagnostic_capture: list[tuple[str, Any]],
) -> None:
    session = MagicMock()
    session.query.return_value.filter_by.return_value.first.return_value = None
    error = StatementError(SECRET, 'INSERT users', {'password': SECRET}, RuntimeError(SECRET))
    monkeypatch.setattr(auth.user_service, 'create_user', MagicMock(side_effect=error))
    from app.exceptions import ServiceOperationError

    with pytest.raises(ServiceOperationError):
        auth.register_user(session, SECRET, SECRET)
    session.rollback.assert_called_once()
    assert_safe(diagnostic_capture)


def test_generation_invalidation_failure_preserves_business_outcome(
    monkeypatch: pytest.MonkeyPatch,
    diagnostic_capture: list[tuple[str, Any]],
) -> None:
    redis = MagicMock()
    redis.set.side_effect = RuntimeError(SECRET)
    monkeypatch.setattr(cache, 'redis_client', redis)

    cache.invalidate_cache_generation(SECRET)
    redis.set.assert_called_once()
    assert_safe(diagnostic_capture)
