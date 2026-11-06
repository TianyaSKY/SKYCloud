"""已接受的取消在断开连接后仍持久化并向生产者发信号测试."""
import asyncio
import threading
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.features.assistant import router


@pytest.mark.parametrize('stage', ['sql', 'signal'])
@pytest.mark.parametrize('fault', [False, True])
async def test_disconnected_cancel_finishes_owned_sql_and_registry_signal(
    monkeypatch: pytest.MonkeyPatch, stage: str, fault: bool
) -> None:
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    signal_release = asyncio.Event()
    order: list[str] = []

    class Owned:
        def __enter__(self) -> "Owned":
            return self

        def __exit__(self, *args: object) -> None:
            order.append('sql-close')

    def persist(*args: object) -> SimpleNamespace:
        try:
            if stage == 'sql':
                entered.set()
                assert release.wait(5)
                if fault:
                    raise RuntimeError('private-sql-error')
            order.append('sql-commit')
            return SimpleNamespace(to_dict=lambda: {'id': 9})
        finally:
            finished.set()

    async def signal(run_id: int) -> None:
        assert run_id == 9 and order == ['sql-commit', 'sql-close']
        if stage == 'signal':
            entered.set()
            await signal_release.wait()
            if fault:
                order.append('signal-fault')
                raise RuntimeError('private-registry-error')
        order.append('signal')

    monkeypatch.setattr(router, '_require_v2', lambda: None)
    monkeypatch.setattr(router.service, 'request_cancel', persist)
    monkeypatch.setattr(router.service.run_registry, 'request_cancel', signal)
    pending = asyncio.create_task(
        router.cancel_assistant_run(
            9,
            cast(Any, SimpleNamespace(id=1)),
            cast(Any, SimpleNamespace(id=2)),
            cast(Any, Owned),
        )
    )
    error: pytest.ExceptionInfo[asyncio.CancelledError] | None = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        pending.cancel('caller-stop')
        await asyncio.sleep(0.01)
        pending.cancel('repeat-stop')
        await asyncio.sleep(0.01)
        assert not pending.done()
        if stage == 'sql':
            assert not order and not finished.is_set()
        else:
            assert order == ['sql-commit', 'sql-close']
    finally:
        release.set()
        signal_release.set()
        with pytest.raises(asyncio.CancelledError) as err:
            await asyncio.wait_for(pending, 2)
        error = err
        assert await asyncio.to_thread(finished.wait, 2)
    assert error is not None
    assert str(error.value) == 'caller-stop'
    if stage == 'sql' and fault:
        assert order == ['sql-close']
    else:
        assert order == ['sql-commit', 'sql-close', 'signal-fault' if fault else 'signal']
