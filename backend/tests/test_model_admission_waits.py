# pyright: reportPrivateUsage=false
import asyncio
from typing import Any
import pytest
from fastapi import HTTPException
from runtime_common.http_wait import guarded_wait
from runtime_common.http_wait import WaitPolicy



@pytest.mark.parametrize('cause', ['revoke', 'timeout', 'cancel', 'late-response', 'abort-fault'])
async def test_wait_drains_operation_and_cleans_completed_late_response(cause: str) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    calls: list[str] = []
    async def operation() -> str:
        try:
            entered.set()
            if cause not in ('late-response', 'abort-fault'):
                await asyncio.Event().wait()
            return 'opened'
        finally:
            calls.append('operation-close')
    async def check() -> None:
        if cause in ('revoke', 'late-response', 'abort-fault'):
            raise HTTPException(401, 'revoked')
    async def abort() -> None:
        calls.append('abort')
        if cause == 'abort-fault':
            raise RuntimeError('private-cleanup-url')
        if cause == 'cancel':
            await release.wait()
    pending = asyncio.create_task(guarded_wait(operation(), WaitPolicy(
        check=check, seconds=0.03, status=504,
        detail='timeout', interval=0.01, on_abort=abort)))
    await entered.wait()
    if cause == 'cancel':
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 1)
    else:
        with pytest.raises(HTTPException) as error:
            await asyncio.wait_for(pending, 1)
        assert error.value.status_code == (504 if cause == 'timeout' else 401)
    assert calls.count('abort') == 1 and calls.count('operation-close') == 1


async def test_successful_wait_transfers_resource_without_abort() -> None:
    calls: list[str] = []
    async def operation() -> str:
        return 'opened'
    async def check() -> None:
        calls.append('check')
    async def abort() -> None:
        pytest.fail('successful wait must retain upstream')
    assert await guarded_wait(operation(), WaitPolicy(check=check, seconds=1, status=504, detail='timeout', on_abort=abort)) == 'opened'
    assert calls == ['check']


@pytest.mark.parametrize('handled_error', [False, True])
@pytest.mark.parametrize('mode', ['asyncio', 'anyio'])
@pytest.mark.parametrize('close_fault', [None, RuntimeError, asyncio.CancelledError])
async def test_successful_wait_cancelled_during_teardown_closes_before_cancellation(monkeypatch: pytest.MonkeyPatch, mode: str, close_fault: Any, handled_error: bool) -> None:
    import anyio
    from runtime_common import http_wait
    calls: list[str] = []
    logs: list[str] = []
    scopes: list[anyio.CancelScope] = []
    def log_error(message: str, *args: Any) -> None:
        logs.append(message % args)
    monkeypatch.setattr(
        http_wait.logger,
        'error',
        log_error,
    )

    async def operation() -> str:
        calls.append('operation')
        return 'opened'
    async def check() -> None:
        if mode == 'anyio':
            scopes[0].cancel()
        else:
            task = asyncio.current_task()
            assert task is not None
            asyncio.get_running_loop().call_soon(task.cancel)
    async def abort() -> None:
        calls.append('abort')
        if close_fault:
            raise close_fault('private-cleanup-credential')
    async def run() -> None:
        with anyio.CancelScope() as scope:
            scopes.append(scope)
            async def wait() -> None:
                await guarded_wait(operation(), WaitPolicy(check=check, seconds=1, status=504, detail='timeout', on_abort=abort))
                calls.append('transferred')
            if handled_error:
                try:
                    raise ValueError('already-handled-caller-error')
                except ValueError:
                    await wait()
            else:
                await wait()
        assert scope.cancelled_caught
    pending = asyncio.create_task(run())
    if mode == 'asyncio':
        with pytest.raises(asyncio.CancelledError) as error:
            await asyncio.wait_for(pending, 1)
        assert 'private-cleanup-credential' not in str(error.value)
    else:
        await asyncio.wait_for(pending, 1)
    assert calls == ['operation', 'abort']
    assert logs == ([] if close_fault is None else [f'HTTP admission cleanup failed (error_type={close_fault.__name__})'])


@pytest.mark.parametrize('phase', ['auth', 'operation'])
async def test_teardown_cancellation_preserves_existing_wait_failure(phase: str) -> None:
    calls: list[str] = []
    original = HTTPException(401, 'fixed-auth') if phase == 'auth' else ValueError('fixed-operation')
    async def operation() -> str:
        if phase == 'operation':
            raise original
        return 'opened'
    async def check() -> None:
        task = asyncio.current_task()
        assert task is not None
        asyncio.get_running_loop().call_soon(task.cancel)
        if phase == 'auth':
            raise original
    async def abort() -> None:
        calls.append('abort')
    with pytest.raises(type(original)) as error:
        await guarded_wait(operation(), WaitPolicy(check=check, seconds=1, status=504, detail='timeout', on_abort=abort))
    assert error.value is original and calls == ['abort']


@pytest.mark.parametrize('outcome', ['timeout', 'cancel'])
async def test_admission_closes_response_published_during_cancel_drain(outcome: str) -> None:
    from runtime_common.http_stream import UpstreamLease
    entered = asyncio.Event()
    calls: list[str] = []
    class Resource:
        def __init__(self, name: str) -> None:
            self.name = name
            self.closed = False
        async def aclose(self) -> None:
            self.closed = True
            calls.append(self.name)
    client, late = Resource('client'), Resource('response')
    lease = UpstreamLease(client)
    async def operation() -> Any:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # 即使已被取消, 提供商仍可能执行完成. 准入控制也持有
            # 该延迟响应, 随后才向下游传递任何数据.
            await asyncio.sleep(.01)
            lease.upstream = late
            return late
    async def check() -> None:
        pass
    pending = asyncio.create_task(guarded_wait(operation(), WaitPolicy(check=check,
        seconds=.02 if outcome == 'timeout' else 60, status=504, detail='timeout', on_abort=lease.close)))
    await asyncio.wait_for(entered.wait(), 1)
    if outcome == 'cancel':
        pending.cancel()
    with pytest.raises(HTTPException if outcome == 'timeout' else asyncio.CancelledError):
        await asyncio.wait_for(pending, 1)
    assert late.closed and client.closed
    assert calls == ['response', 'client']
