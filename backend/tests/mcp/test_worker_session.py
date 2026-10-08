"""Cleanup faults must preserve business outcomes without logging SQL secrets."""
from unittest.mock import MagicMock

import pytest
from loguru import logger
from sqlalchemy.exc import StatementError

from app.mcp import server
from app.mcp.unit_of_work import worker_session


@pytest.mark.parametrize('business_failure', [False, True])
@pytest.mark.parametrize('invalidation_failure', [False, True])
async def test_cleanup_fault_preserves_outcome(monkeypatch, business_failure, invalidation_failure):
    private = 'private-sql-bind-sentinel'
    session = MagicMock()
    session.close.side_effect = StatementError(private, 'SELECT :password', {'password': private}, ValueError(private))
    if invalidation_failure:
        session.invalidate.side_effect = ValueError(private)
    monkeypatch.setattr(server, 'SessionLocal', lambda: session)
    records = []
    sink = logger.add(lambda message: records.append(message.record))
    original = RuntimeError('business error')
    def operation(db):
        assert db is session
        if business_failure:
            raise original
        return {'committed': True}
    try:
        if business_failure:
            with pytest.raises(RuntimeError) as caught:
                await server._run_sync(operation)
            assert caught.value is original
        else:
            assert await server._run_sync(operation) == {'committed': True}
    finally:
        logger.remove(sink)
    session.close.assert_called_once_with()
    session.invalidate.assert_called_once_with()
    session.commit.assert_not_called()
    session.rollback.assert_not_called()
    assert len(records) == (2 if invalidation_failure else 1)
    assert all(private not in record['message'] and record['exception'] is None for record in records)


def test_normal_cleanup_retains_caller_transaction_control():
    session = MagicMock()
    with worker_session(lambda: session) as db:
        assert db is session
    session.close.assert_called_once_with()
    session.invalidate.assert_not_called()
    session.commit.assert_not_called()
    session.rollback.assert_not_called()


def test_construction_failure_preserves_original_exception():
    original = RuntimeError('factory failure')
    def factory():
        raise original
    with pytest.raises(RuntimeError) as caught:
        with worker_session(factory):
            pytest.fail('No session was created')
    assert caught.value is original
