import asyncio
import threading
import pytest
from fastapi import HTTPException
from starlette.requests import Request
from app.features.runtime_llm import router


@pytest.mark.parametrize('cause',['revoked','deadline'])
async def test_quiet_model_wait_rechecks_and_closes_pending_read(monkeypatch,cause):
    entered,closed=asyncio.Event(),asyncio.Event()
    checks=[]
    loop_thread=threading.get_ident()
    class Response:
        async def aiter_bytes(self):
            try:
                entered.set();await asyncio.Event().wait();yield b'never'
            finally:closed.set()
    def validate(request,factory,**kwargs):
        assert threading.get_ident()!=loop_thread
        checks.append(True)
        if cause=='revoked':raise HTTPException(401,'revoked')
    monkeypatch.setattr(router,'_validate_current_credential',validate)
    monkeypatch.setattr(router,'RECHECK_SECONDS',0.01)
    monkeypatch.setattr(router,'MAX_STREAM_SECONDS',0.03)
    async def receive():await asyncio.Event().wait()
    request=Request({'type':'http','headers':[]},receive)
    async def collect():return [part async for part in router.authorized_bytes(Response(),request,None)]
    with pytest.raises(HTTPException) as error:await asyncio.wait_for(collect(),1)
    assert error.value.status_code==(401 if cause=='revoked' else 502)
    assert checks and entered.is_set() and closed.is_set()


async def test_disconnected_model_client_closes_pending_reader_without_sql(monkeypatch):
    closed=asyncio.Event()
    class Response:
        async def aiter_bytes(self):
            try:await asyncio.Event().wait();yield b'never'
            finally:closed.set()
    async def receive():return {'type':'http.disconnect'}
    request=Request({'type':'http','headers':[]},receive)
    def validate(*args,**kwargs):pytest.fail('disconnected client must stop before SQL')
    monkeypatch.setattr(router,'_validate_current_credential',validate)
    monkeypatch.setattr(router,'RECHECK_SECONDS',0.01)
    async def collect():return [part async for part in router.authorized_bytes(Response(),request,None)]
    with pytest.raises(HTTPException) as error:await asyncio.wait_for(collect(),1)
    assert error.value.status_code==499 and closed.is_set()


@pytest.mark.parametrize('cause',['revoked','disconnect','deadline','cancel','eof'])
@pytest.mark.parametrize('close_error',[RuntimeError,asyncio.CancelledError])
async def test_model_iterator_cleanup_fault_preserves_outcome(monkeypatch,cause,close_error):
    started=asyncio.Event()
    calls,logs=[],[]
    from app.features.runtime_llm import waits
    monkeypatch.setattr(waits.logger,'error',lambda message,*args:logs.append(message.format(*args)))
    class Iterator:
        def __aiter__(self):return self
        async def __anext__(self):
            try:
                started.set()
                if cause=='eof':raise StopAsyncIteration
                await asyncio.Event().wait()
            finally:calls.append('read-finished')
        async def aclose(self):
            calls.append('close')
            raise close_error('private-provider-credential')
    class Response:
        def aiter_bytes(self):return Iterator()
    async def verify(*args):
        if cause in ('revoked','disconnect'):raise HTTPException(401 if cause=='revoked' else 499,'fixed')
    monkeypatch.setattr(router,'verify_connected',verify)
    monkeypatch.setattr(router,'RECHECK_SECONDS',0.01)
    monkeypatch.setattr(router,'MAX_STREAM_SECONDS',0.03)
    async def collect():return [part async for part in router.authorized_bytes(Response(),None,None)]
    pending=asyncio.create_task(collect())
    await started.wait()
    if cause=='cancel':
        pending.cancel()
        with pytest.raises(asyncio.CancelledError) as error:await asyncio.wait_for(pending,1)
        assert 'private-provider-credential' not in str(error.value)
    elif cause=='eof':assert await asyncio.wait_for(pending,1)==[]
    else:
        with pytest.raises(HTTPException) as error:await asyncio.wait_for(pending,1)
        assert error.value.status_code=={'revoked':401,'disconnect':499,'deadline':502}[cause]
    assert calls==['read-finished','close']
    assert logs==[f'Model iterator close failed (error_type={close_error.__name__})']


async def test_model_repeated_cancel_drains_reader_before_iterator_close(monkeypatch):
    started,cleaning,release,closed=(asyncio.Event() for _ in range(4))
    calls=[]
    class Iterator:
        def __aiter__(self):return self
        async def __anext__(self):
            try:started.set();await asyncio.Event().wait()
            finally:
                cleaning.set();await release.wait();closed.set();calls.append('read-finished')
        async def aclose(self):
            assert closed.is_set()
            calls.append('close')
    class Response:
        def aiter_bytes(self):return Iterator()
    async def verify(*args):pass
    monkeypatch.setattr(router,'verify_connected',verify)
    async def collect():return [part async for part in router.authorized_bytes(Response(),None,None)]
    pending=asyncio.create_task(collect())
    try:
        await started.wait();pending.cancel();await asyncio.wait_for(cleaning.wait(),1)
        pending.cancel();await asyncio.sleep(0.01)
        assert not pending.done(),'repeated cancellation interrupted reader cleanup'
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):await asyncio.wait_for(pending,1)
    assert calls==['read-finished','close']


async def test_model_cancel_during_successful_eof_cleanup_is_preserved(monkeypatch):
    closing,release=asyncio.Event(),asyncio.Event()
    calls=[]
    class Iterator:
        def __aiter__(self):return self
        async def __anext__(self):raise StopAsyncIteration
        async def aclose(self):
            closing.set();await release.wait();calls.append('close')
    class Response:
        def aiter_bytes(self):return Iterator()
    async def verify(*args):pass
    monkeypatch.setattr(router,'verify_connected',verify)
    async def collect():return [part async for part in router.authorized_bytes(Response(),None,None)]
    pending=asyncio.create_task(collect())
    try:
        await asyncio.wait_for(closing.wait(),1)
        pending.cancel();await asyncio.sleep(0.01)
        assert not pending.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):await asyncio.wait_for(pending,1)
    assert calls==['close']


async def test_model_anyio_scope_cancel_drains_reader(monkeypatch):
    import anyio
    started,cleaning,release=(asyncio.Event() for _ in range(3))
    calls,scopes=[],[]
    class Iterator:
        def __aiter__(self):return self
        async def __anext__(self):
            try:started.set();await asyncio.Event().wait()
            finally:cleaning.set();await release.wait();calls.append('read-finished')
        async def aclose(self):calls.append('close')
    class Response:
        def aiter_bytes(self):return Iterator()
    async def verify(*args):pass
    monkeypatch.setattr(router,'verify_connected',verify)
    async def collect():
        with anyio.CancelScope() as scope:
            scopes.append(scope)
            async for _ in router.authorized_bytes(Response(),None,None):pass
        assert scope.cancelled_caught
    pending=asyncio.create_task(collect())
    try:
        await started.wait();scopes[0].cancel()
        await asyncio.wait_for(cleaning.wait(),1)
        await asyncio.sleep(0.01);assert not pending.done()
    finally:
        release.set();await asyncio.wait_for(pending,1)
    assert calls==['read-finished','close']
