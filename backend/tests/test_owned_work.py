# pyright: reportPrivateUsage=false
import asyncio
import contextvars
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast
import anyio
import pytest
from app.infra.owned_work import run_owned, run_owned_persistent


@pytest.mark.parametrize('fault', [False, True])
async def test_started_work_context_and_cleanup_survive_repeated_cancel(fault: bool) -> None:
    entered, release = threading.Event(), threading.Event()
    actor: contextvars.ContextVar[int | None] = contextvars.ContextVar('actor', default=None)
    calls: list[str] = []
    def work() -> int:
        assert actor.get() == 7
        entered.set()
        assert release.wait(3)
        calls.append('closed')
        if fault:
            raise RuntimeError('private-worker-error')
        return 12
    token = actor.set(7)
    try:
        pending = asyncio.create_task(run_owned(work))
    finally:
        actor.reset(token)
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        pending.cancel('caller-stop')
        await asyncio.sleep(.01)
        pending.cancel('second-stop')
        await asyncio.sleep(.01)
        assert not pending.done() and not calls
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError) as error:
            await asyncio.wait_for(pending, 1)
    assert str(error.value) == 'caller-stop' and calls == ['closed']
    assert actor.get() is None


@pytest.mark.parametrize('persistent', [False, True])
async def test_queued_work_policy_on_actual_single_thread_executor(monkeypatch: pytest.MonkeyPatch, persistent: bool) -> None:
    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
    entered, release = threading.Event(), threading.Event()
    queued = asyncio.Event()
    calls: list[str] = []
    actual_to_thread = asyncio.to_thread
    def hog() -> None:
        entered.set()
        assert release.wait(3)
    blocked = asyncio.create_task(actual_to_thread(hog))
    while not entered.is_set():
        await asyncio.sleep(.001)
    async def observed(function: Any, *args: Any, **kwargs: Any) -> Any:
        queued.set()
        return await actual_to_thread(function, *args, **kwargs)
    monkeypatch.setattr(asyncio, 'to_thread', observed)
    worker = run_owned_persistent if persistent else run_owned
    pending = asyncio.create_task(worker(lambda: calls.append('effect')))
    try:
        await asyncio.wait_for(queued.wait(), 1)
        pending.cancel()
        await asyncio.sleep(.01)
        assert not pending.done() and not calls
    finally:
        release.set()
        await blocked
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 1)
    assert calls == (['effect'] if persistent else [])


async def test_anyio_scope_drains_started_work_before_cancellation_delivery() -> None:
    entered, release = threading.Event(), threading.Event()
    scopes: list[anyio.CancelScope] = []
    calls: list[str] = []
    def work() -> None:
        entered.set()
        assert release.wait(3)
        calls.append('closed')
    async def caller() -> None:
        with anyio.CancelScope() as scope:
            scopes.append(scope)
            await run_owned(work)
            calls.append('delivered')
        assert scope.cancelled_caught
    pending = asyncio.create_task(caller())
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        scopes[0].cancel()
        await asyncio.sleep(.01)
        assert not pending.done() and not calls
    finally:
        release.set()
        await asyncio.wait_for(pending, 1)
    assert calls == ['closed']


async def test_audit_cancellation_does_not_replace_existing_tool_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.mcp import audit
    from app.exceptions import BusinessRuleError
    entered, release = threading.Event(), threading.Event()
    calls: list[str] = []
    original = BusinessRuleError('fixed-domain-error')
    def record(**kwargs: Any) -> None:
        entered.set()
        assert release.wait(3)
        calls.append(kwargs['error_type'])
    monkeypatch.setattr(audit, 'record_mcp_call', record)
    @audit.audited_tool('test')
    async def tool() -> None:
        raise original
    pending = asyncio.create_task(cast(Any, tool()))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        pending.cancel()
        await asyncio.sleep(.01)
        assert not pending.done()
    finally:
        release.set()
        with pytest.raises(BusinessRuleError) as error:
            await asyncio.wait_for(pending, 1)
    assert error.value is original and calls == ['BusinessRuleError']
