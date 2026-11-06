"""工作线程权限通知必须遵循 commit 与事件循环所有权."""
import asyncio
import threading
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest

from app.features.assistant import service
from app.features.assistant.run_control import RunController


async def test_worker_permission_notification_wakes_owning_loop() -> None:
    loop = asyncio.get_running_loop()
    original_debug = loop.get_debug()
    loop.set_debug(True)  # asyncio rejects unsafe cross-thread Future.set_result.
    try:
        controller = RunController()
        future = controller.wait_for_permission('p1')
        async def wait() -> str:
            return await future
        waiter = asyncio.create_task(wait())
        await asyncio.sleep(0)
        assert await asyncio.to_thread(controller.resolve_permission, 'p1', 'once')
        assert await asyncio.wait_for(waiter, 1) == 'once'
        assert not controller.resolve_permission('p1', 'reject')
    finally:
        loop.set_debug(original_debug)


async def test_cancelled_future_ignores_queued_worker_notification() -> None:
    controller = RunController()
    future = controller.wait_for_permission('p1')
    # 在单个回调中完成调度与取消, 位于排队通知投递之前.
    def schedule_then_cancel() -> None:
        worker = threading.Thread(target=lambda: controller.resolve_permission('p1', 'once'))
        worker.start()
        worker.join(timeout=1)
        assert not worker.is_alive()
        future.cancel()
    schedule_then_cancel()
    await asyncio.sleep(0)
    assert future.cancelled()


@pytest.mark.parametrize('commit_fails', [False, True])
def test_permission_notification_only_after_successful_commit(monkeypatch: pytest.MonkeyPatch, commit_fails: bool) -> None:
    run = SimpleNamespace(
        engine="expert",
        status="waiting_permission",
        pending_permission_id="p1",
    )
    def mock_run(*args: Any) -> Any:
        return run
    def mock_none(*args: Any) -> None:
        pass
    def mock_true(*args: Any) -> bool:
        return True

    monkeypatch.setattr(service.repository, "get_owned_run", mock_run)
    monkeypatch.setattr(service.repository, "lock_run", mock_run)
    monkeypatch.setattr(service, "assert_can_write", mock_none)
    monkeypatch.setattr(service, "assert_member_locked", mock_none)
    monkeypatch.setattr(service.repository, "answer_permission", mock_true)
    committed: list[bool] = []
    def commit() -> None:
        if commit_fails:
            raise RuntimeError('database unavailable')
        committed.append(True)
    session = cast(Any, SimpleNamespace(commit=commit))
    def notify_side_effect(*args: Any) -> bool:
        if committed != [True]:
            pytest.fail('not committed')
        return True
    notify = Mock(side_effect=notify_side_effect)
    monkeypatch.setattr(service.run_registry, 'resolve_permission', notify)
    if commit_fails:
        with pytest.raises(RuntimeError, match='database unavailable'):
            service.respond_permission(
                session,
                service.RespondPermissionParams(
                    workspace=cast(Any, SimpleNamespace(id=1)),
                    user_id=1,
                    run_id=2,
                    permission_id='p1',
                    response='once',
                ),
            )
        notify.assert_not_called()
    else:
        assert service.respond_permission(
            session,
            service.RespondPermissionParams(
                workspace=cast(Any, SimpleNamespace(id=1)),
                user_id=1,
                run_id=2,
                permission_id='p1',
                response='once',
            ),
        ) is run
        notify.assert_called_once_with(2, 'p1', 'once')

