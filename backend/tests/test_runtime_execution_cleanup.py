"""生命周期锁清理必须保留已接受结果与主要故障测试."""
from types import SimpleNamespace
from typing import Any

import pytest
from loguru import logger

from app.exceptions import PermissionDeniedError
from app.features.workspace.runtime_execution import execution


@pytest.mark.parametrize('phase', ['unlock', 'invalidate', 'close'])
@pytest.mark.parametrize('denied', [False, True])
def test_cleanup_keeps_business_outcome_and_safe_diagnostics(phase: str, denied: bool) -> None:
    private = 'PRIVATE_DATABASE_CONNECTION_SENTINEL'
    calls: list[str] = []
    logs: list[tuple[str, Any]] = []
    original = PermissionDeniedError('permission revoked')

    class Connection:
        def scalar(self, *args: object) -> bool:
            return True

        def commit(self) -> None:
            calls.append('commit')

        def execute(self, *args: object) -> None:
            calls.append('unlock')
            if phase != 'close':
                raise RuntimeError(private)

        def invalidate(self) -> None:
            calls.append('invalidate')
            if phase == 'invalidate':
                raise OSError(private)

        def close(self) -> None:
            calls.append('close')
            if phase == 'close':
                raise OSError(private)

    connection = Connection()
    bind = SimpleNamespace(
        dialect=SimpleNamespace(name='postgresql'),
        engine=SimpleNamespace(connect=lambda: connection),
    )
    session = SimpleNamespace(get_bind=lambda: bind)

    def log_sink(message: Any) -> None:
        logs.append((str(message), message.record['exception']))

    sink = logger.add(log_sink)
    try:
        if denied:
            with pytest.raises(PermissionDeniedError) as error:
                with execution(session, 12, 34):  # type: ignore[arg-type] # pyright: ignore[reportArgumentType]
                    raise original
            assert error.value is original
        else:
            with execution(session, 12, 34):  # type: ignore[arg-type] # pyright: ignore[reportArgumentType]
                calls.append('accepted')
            assert 'accepted' in calls
    finally:
        logger.remove(sink)
    assert 'invalidate' in calls and calls.count('close') == 1
    assert logs and all(private not in message and exception is None for message, exception in logs)
