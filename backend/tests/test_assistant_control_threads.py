"""Worker-thread permission notifications must respect commit and loop ownership."""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.features.assistant import service


async def test_worker_permission_notification_wakes_owning_loop():
    loop = asyncio.get_running_loop()
    original_debug = loop.get_debug()
    loop.set_debug(True)  # asyncio rejects unsafe cross-thread Future.set_result.
    try:
        controller = service.RunController()
        future = controller.wait_for_permission('p1')
        async def wait():
            return await future
        waiter = asyncio.create_task(wait())
        await asyncio.sleep(0)
        assert await asyncio.to_thread(controller.resolve_permission, 'p1', 'once')
        assert await asyncio.wait_for(waiter, 1) == 'once'
        assert not controller.resolve_permission('p1', 'reject')
    finally:
        loop.set_debug(original_debug)


async def test_cancelled_future_ignores_queued_worker_notification():
    controller = service.RunController()
    future = controller.wait_for_permission('p1')
    # Schedule and cancel in one callback, before queued notification delivery.
    def schedule_then_cancel():
        import threading
        worker = threading.Thread(target=lambda: controller.resolve_permission('p1', 'once'))
        worker.start()
        worker.join(timeout=1)
        assert not worker.is_alive()
        future.cancel()
    schedule_then_cancel()
    await asyncio.sleep(0)
    assert future.cancelled()


@pytest.mark.parametrize('commit_fails', [False, True])
def test_permission_notification_only_after_successful_commit(monkeypatch, commit_fails):
    run = SimpleNamespace(engine='expert', status='waiting_permission', pending_permission_id='p1')
    monkeypatch.setattr(service.repository, 'get_owned_run', lambda *args: run)
    monkeypatch.setattr(service, 'assert_can_write', lambda *args: None)
    monkeypatch.setattr(service, 'assert_member_locked', lambda *args: None)
    monkeypatch.setattr(service.repository, 'set_permission_response', lambda *args: True)
    committed = []
    def commit():
        if commit_fails:
            raise RuntimeError('database unavailable')
        committed.append(True)
    session = SimpleNamespace(commit=commit)
    notify = Mock(side_effect=lambda *args: committed == [True] or pytest.fail('not committed'))
    monkeypatch.setattr(service.run_registry, 'resolve_permission', notify)
    if commit_fails:
        with pytest.raises(RuntimeError, match='database unavailable'):
            service.respond_permission(session, SimpleNamespace(id=1), 1, 2, 'p1', 'once')
        notify.assert_not_called()
    else:
        assert service.respond_permission(session, SimpleNamespace(id=1), 1, 2, 'p1', 'once') is run
        notify.assert_called_once_with(2, 'p1', 'once')
