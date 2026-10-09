import asyncio
import pytest
from fastapi import HTTPException
from app.infra.http_wait import guarded_wait


@pytest.fixture(autouse=True, params=["backend", "controller"])
def wait_surface(request, monkeypatch):
    if request.param == "controller":
        import sys
        from runtime_controller import http_wait
        monkeypatch.setattr(sys.modules[__name__], "guarded_wait", http_wait.guarded_wait)


@pytest.mark.parametrize('cause',['revoke','timeout','cancel','late-response','abort-fault'])
async def test_wait_drains_operation_and_cleans_completed_late_response(cause):
    entered,release=asyncio.Event(),asyncio.Event()
    calls=[]
    async def operation():
        try:
            entered.set()
            if cause not in ('late-response','abort-fault'):await asyncio.Event().wait()
            return 'opened'
        finally:calls.append('operation-close')
    async def check():
        if cause in ('revoke','late-response','abort-fault'):raise HTTPException(401,'revoked')
    async def abort():
        calls.append('abort')
        if cause=='abort-fault':raise RuntimeError('private-cleanup-url')
        if cause=='cancel':await release.wait()
    pending=asyncio.create_task(guarded_wait(operation(),check=check,seconds=0.03,status=504,
        detail='timeout',interval=0.01,on_abort=abort))
    await entered.wait()
    if cause=='cancel':
        pending.cancel();await asyncio.sleep(0)
        pending.cancel();await asyncio.sleep(0)
        assert not pending.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):await asyncio.wait_for(pending,1)
    else:
        with pytest.raises(HTTPException) as error:await asyncio.wait_for(pending,1)
        assert error.value.status_code==(504 if cause=='timeout' else 401)
    assert calls.count('abort')==1 and calls.count('operation-close')==1


async def test_successful_wait_transfers_resource_without_abort():
    calls=[]
    async def operation():return 'opened'
    async def check():calls.append('check')
    async def abort():pytest.fail('successful wait must retain upstream')
    assert await guarded_wait(operation(),check=check,seconds=1,status=504,detail='timeout',on_abort=abort)=='opened'
    assert calls==['check']


@pytest.mark.parametrize('handled_error',[False,True])
@pytest.mark.parametrize('mode',['asyncio','anyio'])
@pytest.mark.parametrize('close_fault',[None,RuntimeError,asyncio.CancelledError])
async def test_successful_wait_cancelled_during_teardown_closes_before_cancellation(monkeypatch,mode,close_fault,handled_error):
    import anyio
    from app.infra import http_wait
    calls,logs,scopes=[],[],[]
    if guarded_wait.__module__.startswith('runtime_controller'):
        from runtime_controller import http_wait as standalone
        monkeypatch.setattr(standalone.logging,'error',lambda message,*args:logs.append(message % args))
    else:
        monkeypatch.setattr(http_wait.logger,'error',lambda message,*args:logs.append(message.format(*args)))
    async def operation():calls.append('operation');return 'opened'
    async def check():
        if mode=='anyio':scopes[0].cancel()
        else:asyncio.get_running_loop().call_soon(asyncio.current_task().cancel)
    async def abort():
        calls.append('abort')
        if close_fault:raise close_fault('private-cleanup-credential')
    async def run():
        with anyio.CancelScope() as scope:
            scopes.append(scope)
            async def wait():
                await guarded_wait(operation(),check=check,seconds=1,status=504,detail='timeout',on_abort=abort)
                calls.append('transferred')
            if handled_error:
                try:raise ValueError('already-handled-caller-error')
                except ValueError:await wait()
            else:await wait()
        assert scope.cancelled_caught
    pending=asyncio.create_task(run())
    if mode=='asyncio':
        with pytest.raises(asyncio.CancelledError) as error:await asyncio.wait_for(pending,1)
        assert 'private-cleanup-credential' not in str(error.value)
    else:await asyncio.wait_for(pending,1)
    assert calls==['operation','abort']
    assert logs==([] if close_fault is None else [f'HTTP admission cleanup failed (error_type={close_fault.__name__})'])


@pytest.mark.parametrize('phase',['auth','operation'])
async def test_teardown_cancellation_preserves_existing_wait_failure(phase):
    calls=[]
    original=HTTPException(401,'fixed-auth') if phase=='auth' else ValueError('fixed-operation')
    async def operation():
        if phase=='operation':raise original
        return 'opened'
    async def check():
        asyncio.get_running_loop().call_soon(asyncio.current_task().cancel)
        if phase=='auth':raise original
    async def abort():calls.append('abort')
    with pytest.raises(type(original)) as error:
        await guarded_wait(operation(),check=check,seconds=1,status=504,detail='timeout',on_abort=abort)
    assert error.value is original and calls==['abort']


@pytest.mark.parametrize('outcome',['timeout','cancel'])
async def test_admission_closes_response_published_during_cancel_drain(outcome):
    from app.infra.http_stream import UpstreamLease
    from fastapi import HTTPException
    entered=asyncio.Event()
    calls=[]
    class Resource:
        def __init__(self,name):self.name=name;self.closed=False
        async def aclose(self):self.closed=True;calls.append(self.name)
    client,late=Resource('client'),Resource('response')
    lease=UpstreamLease(client)
    async def operation():
        entered.set()
        try:await asyncio.Event().wait()
        except asyncio.CancelledError:
            # A provider can complete despite cancellation. Admission owns
            # that late response too, before transferring anything downstream.
            await asyncio.sleep(.01)
            lease.upstream=late
            return late
    async def check():pass
    pending=asyncio.create_task(guarded_wait(operation(),check=check,
        seconds=.02 if outcome=='timeout' else 60,status=504,detail='timeout',on_abort=lease.close))
    await asyncio.wait_for(entered.wait(),1)
    if outcome=='cancel':pending.cancel()
    with pytest.raises(HTTPException if outcome=='timeout' else asyncio.CancelledError):
        await asyncio.wait_for(pending,1)
    assert late.closed and client.closed
    assert calls==['response','client']
