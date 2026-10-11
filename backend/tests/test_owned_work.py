import asyncio
import contextvars
import threading
from concurrent.futures import ThreadPoolExecutor
import anyio
import pytest
from app.infra.owned_work import run_owned


@pytest.mark.parametrize('fault',[False,True])
async def test_started_work_context_and_cleanup_survive_repeated_cancel(fault):
    entered,release=threading.Event(),threading.Event()
    actor=contextvars.ContextVar('actor',default=None)
    calls=[]
    def work():
        assert actor.get()==7
        entered.set();assert release.wait(3);calls.append('closed')
        if fault:raise RuntimeError('private-worker-error')
        return 12
    token=actor.set(7)
    try:pending=asyncio.create_task(run_owned(work))
    finally:actor.reset(token)
    try:
        assert await asyncio.to_thread(entered.wait,1)
        pending.cancel('caller-stop');await asyncio.sleep(.01)
        pending.cancel('second-stop');await asyncio.sleep(.01)
        assert not pending.done() and not calls
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError) as error:await asyncio.wait_for(pending,1)
    assert str(error.value)=='caller-stop' and calls==['closed']
    assert actor.get() is None


@pytest.mark.parametrize('cancel_queued',[True,False])
async def test_queued_work_policy_on_actual_single_thread_executor(monkeypatch,cancel_queued):
    loop=asyncio.get_running_loop();loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
    entered,release=threading.Event(),threading.Event()
    queued=asyncio.Event();calls=[]
    actual_to_thread=asyncio.to_thread
    def hog():entered.set();assert release.wait(3)
    blocked=asyncio.create_task(actual_to_thread(hog))
    while not entered.is_set():await asyncio.sleep(.001)
    async def observed(function,*args,**kwargs):
        queued.set()
        return await actual_to_thread(function,*args,**kwargs)
    monkeypatch.setattr(asyncio,'to_thread',observed)
    pending=asyncio.create_task(run_owned(lambda:calls.append('effect'),cancel_queued=cancel_queued))
    try:
        await asyncio.wait_for(queued.wait(),1)
        pending.cancel();await asyncio.sleep(.01)
        assert not pending.done() and not calls
    finally:
        release.set();await blocked
        with pytest.raises(asyncio.CancelledError):await asyncio.wait_for(pending,1)
    assert calls==([] if cancel_queued else ['effect'])


async def test_anyio_scope_drains_started_work_before_cancellation_delivery():
    entered,release=threading.Event(),threading.Event()
    scopes=[];calls=[]
    def work():entered.set();assert release.wait(3);calls.append('closed')
    async def caller():
        with anyio.CancelScope() as scope:
            scopes.append(scope)
            await run_owned(work)
            calls.append('delivered')
        assert scope.cancelled_caught
    pending=asyncio.create_task(caller())
    try:
        assert await asyncio.to_thread(entered.wait,1)
        scopes[0].cancel();await asyncio.sleep(.01)
        assert not pending.done() and not calls
    finally:release.set();await asyncio.wait_for(pending,1)
    assert calls==['closed']


async def test_audit_cancellation_does_not_replace_existing_tool_failure(monkeypatch):
    from app.mcp import audit
    from app.exceptions import BusinessRuleError
    entered,release=threading.Event(),threading.Event()
    calls=[]
    original=BusinessRuleError('fixed-domain-error')
    def record(**kwargs):entered.set();assert release.wait(3);calls.append(kwargs['error_type'])
    monkeypatch.setattr(audit,'record_mcp_call',record)
    @audit.audited_tool('test')
    async def tool():raise original
    pending=asyncio.create_task(tool())
    try:
        assert await asyncio.to_thread(entered.wait,1)
        pending.cancel();await asyncio.sleep(.01)
        assert not pending.done()
    finally:
        release.set()
        with pytest.raises(BusinessRuleError) as error:await asyncio.wait_for(pending,1)
    assert error.value is original and calls==['BusinessRuleError']
