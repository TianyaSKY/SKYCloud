"""Lifecycle lock cleanup must retain accepted results and primary failures."""
from types import SimpleNamespace

import pytest
from loguru import logger

from app.exceptions import PermissionDeniedError
from app.features.workspace.runtime_execution import execution


@pytest.mark.parametrize('phase', ['unlock', 'invalidate', 'close'])
@pytest.mark.parametrize('denied', [False, True])
def test_cleanup_keeps_business_outcome_and_safe_diagnostics(phase, denied):
    private = 'PRIVATE_DATABASE_CONNECTION_SENTINEL'
    calls, logs = [], []
    original = PermissionDeniedError('permission revoked')

    class Connection:
        def scalar(self, *args): return True
        def commit(self): calls.append('commit')
        def execute(self, *args):
            calls.append('unlock')
            if phase != 'close': raise RuntimeError(private)
        def invalidate(self):
            calls.append('invalidate')
            if phase == 'invalidate': raise OSError(private)
        def close(self):
            calls.append('close')
            if phase == 'close': raise OSError(private)

    connection = Connection()
    bind = SimpleNamespace(dialect=SimpleNamespace(name='postgresql'),
                           engine=SimpleNamespace(connect=lambda: connection))
    session = SimpleNamespace(get_bind=lambda: bind)
    sink = logger.add(lambda message: logs.append((str(message), message.record['exception'])))
    try:
        if denied:
            with pytest.raises(PermissionDeniedError) as error:
                with execution(session, 12, 34): raise original
            assert error.value is original
        else:
            with execution(session, 12, 34): calls.append('accepted')
            assert 'accepted' in calls
    finally:
        logger.remove(sink)
    assert 'invalidate' in calls and calls.count('close') == 1
    assert logs and all(private not in message and exception is None for message, exception in logs)
