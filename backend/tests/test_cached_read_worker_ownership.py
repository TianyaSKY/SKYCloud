import asyncio
import threading
from contextlib import contextmanager
import pytest
from app.features.auth import user_service
from app.features.sys_dict import service as dictionary


@pytest.mark.parametrize('operation', ['user-owned', 'user-shared', 'dictionary'])
@pytest.mark.parametrize('fault', [False, True])
async def test_cached_sql_read_drains_before_cancellation_returns(monkeypatch, operation, fault):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    closed = []
    session = object()
    @contextmanager
    def factory():
        try:
            yield session
        finally:
            closed.append('closed')
    def read(actual, *args):
        assert actual is session
        entered.set()
        try:
            assert release.wait(3)
            if fault:
                raise RuntimeError('private-query-error')
            return [] if operation == 'dictionary' else {'id': 1, 'username': 'owned', 'role': 'common', 'avatar': None}
        finally:
            finished.set()
    monkeypatch.setattr(user_service, '_get_user_data', read)
    monkeypatch.setattr(user_service.account_repository, 'current_role', lambda *args: 'common')
    monkeypatch.setattr(dictionary, '_get_sys_dict_all_cached', read)
    if operation == 'dictionary':
        task = asyncio.create_task(dictionary.get_sys_dict_all(session))
    elif operation == 'user-owned':
        task = asyncio.create_task(user_service.get_user(None, 1, session_factory=factory))
    else:
        task = asyncio.create_task(user_service.get_user(session, 1))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel('caller-stop')
        await asyncio.sleep(.01)
        task.cancel('repeat-stop')
        await asyncio.sleep(.01)
        assert not task.done() and not finished.is_set() and not closed
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError) as error:
            await asyncio.wait_for(task, 1)
        assert await asyncio.to_thread(finished.wait, 1)
    assert str(error.value) == 'caller-stop'
    assert closed == (['closed'] if operation == 'user-owned' else [])
