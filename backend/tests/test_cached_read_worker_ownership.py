import asyncio
import threading
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any, cast

import pytest

from app.features.auth import user_service


@pytest.mark.parametrize('operation', ['user-owned', 'user-shared'])
@pytest.mark.parametrize('fault', [False, True])
async def test_cached_sql_read_drains_before_cancellation_returns(
    monkeypatch: pytest.MonkeyPatch, operation: str, fault: bool
) -> None:
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    closed: list[str] = []
    session = object()

    @contextmanager
    def factory() -> Generator[object, None, None]:
        try:
            yield session
        finally:
            closed.append('closed')

    def read(actual: object, *args: object) -> dict[str, object]:
        assert actual is session
        entered.set()
        try:
            assert release.wait(3)
            if fault:
                raise RuntimeError('private-query-error')
            return {'id': 1, 'username': 'owned', 'role': 'common', 'avatar': None}
        finally:
            finished.set()

    def _mock_role(*args: object) -> str:
        return 'common'

    monkeypatch.setattr(user_service, 'get_user_data', read)
    monkeypatch.setattr(user_service.account_repository, 'current_role', _mock_role)
    if operation == 'user-owned':
        task = asyncio.create_task(user_service.get_user(None, 1, session_factory=cast(Any, factory)))
    else:
        task = asyncio.create_task(user_service.get_user(cast(Any, session), 1))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel('caller-stop')
        await asyncio.sleep(0.01)
        task.cancel('repeat-stop')
        await asyncio.sleep(0.01)
        assert not task.done() and not finished.is_set() and not closed
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError) as error:
            await asyncio.wait_for(task, 1)
        assert await asyncio.to_thread(finished.wait, 1)
    assert str(error.value) == 'caller-stop'
    assert closed == (['closed'] if operation == 'user-owned' else [])
