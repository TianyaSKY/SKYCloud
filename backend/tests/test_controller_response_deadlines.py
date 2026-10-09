"""Quiet upstream reads must obey a total response deadline, then drain ownership."""
import asyncio
import httpx
import pytest
from unittest.mock import Mock
from runtime_controller import app as controller
from runtime_controller.policy import Settings


@pytest.mark.parametrize('event',[False,True])
async def test_controller_quiet_response_reaches_deadline(monkeypatch,tmp_path,event):
    monkeypatch.setattr(controller,'MAX_RESPONSE_SECONDS',.03,raising=False)
    monkeypatch.setattr(controller,'MAX_EVENT_SECONDS',.03,raising=False)
    engine=Mock();engine.target.return_value=('http://runtime.test',('server','secret'))
    settings=Settings('T'*48,'S'*48,'test/runtime@sha256:'+'a'*64,'test','1g',100000,'test',str(tmp_path))
    closed=[]
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            try:
                yield b'data: allowed\n\n' if event else b'partial'
                await asyncio.Event().wait()
            finally:closed.append('reader')
        async def aclose(self):closed.append('response')
    actual_client=httpx.AsyncClient
    clients=[]
    def client(**kwargs):
        response=httpx.Response(200,headers={'content-type':'text/event-stream' if event else 'application/json'},stream=Stream())
        opened=actual_client(transport=httpx.MockTransport(lambda request:response),**kwargs)
        clients.append(opened);return opened
    monkeypatch.setattr(controller.httpx,'AsyncClient',client)
    async with actual_client(transport=httpx.ASGITransport(app=controller.create_app(settings,engine)),base_url='http://controller.test') as downstream:
        result=await asyncio.wait_for(downstream.get('/runtimes/'+'a'*64+'/http/event',
                       headers={'X-Runtime-Controller-Token':settings.token}),.3)
    assert result.status_code==(200 if event else 502)
    if event:assert result.content==b'data: allowed\n\n'
    else:assert result.json()=={'detail':'Runtime response timed out'}
    assert closed==['reader','response']
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize('stage',['ready','consumer'])
async def test_deadline_never_forwards_late_bytes(monkeypatch,stage):
    from fastapi import HTTPException
    from runtime_controller import http_stream
    now=[0]
    monkeypatch.setattr(http_stream,'monotonic',lambda:now[0])
    calls=[]
    class Iterator:
        def __aiter__(self):return self
        async def __anext__(self):
            calls.append('read')
            if stage=='ready':now[0]=2
            return b'private-late'
        async def aclose(self):calls.append('close')
    reader=http_stream.deadline_reader(Iterator(),1)
    if stage=='consumer':
        assert await anext(reader)==b'private-late'
        now[0]=2
    with pytest.raises(HTTPException) as error:await anext(reader)
    assert error.value.status_code==502
    assert calls==['read','close']


@pytest.mark.parametrize('outcome',['timeout','cancel'])
async def test_deadline_drains_reader_despite_repeated_cancellation(outcome):
    from fastapi import HTTPException
    from runtime_controller.http_stream import deadline_reader
    entered,closing,release=asyncio.Event(),asyncio.Event(),asyncio.Event()
    calls=[]
    class Iterator:
        def __aiter__(self):return self
        async def __anext__(self):
            entered.set()
            try:await asyncio.Event().wait()
            finally:
                closing.set();await release.wait();calls.append('drained')
        async def aclose(self):assert calls==['drained'];calls.append('close')
    pending=asyncio.create_task(anext(deadline_reader(Iterator(),.02 if outcome=='timeout' else 60)))
    try:
        await asyncio.wait_for(entered.wait(),1)
        if outcome=='cancel':pending.cancel()
        await asyncio.wait_for(closing.wait(),1)
        pending.cancel();await asyncio.sleep(0)
        pending.cancel();await asyncio.sleep(.01)
        assert not pending.done() and not calls
    finally:
        release.set()
        with pytest.raises(HTTPException if outcome=='timeout' else asyncio.CancelledError) as error:
            await asyncio.wait_for(pending,1)
        if outcome=='timeout':assert error.value.status_code==502
    assert calls==['drained','close']


@pytest.mark.parametrize('handled',[False,True])
async def test_deadline_eof_cleanup_preserves_new_cancellation(handled):
    from runtime_controller.http_stream import deadline_reader
    closing,release=asyncio.Event(),asyncio.Event()
    calls=[]
    class Iterator:
        def __aiter__(self):return self
        async def __anext__(self):raise StopAsyncIteration
        async def aclose(self):closing.set();await release.wait();calls.append('close')
    async def consume():
        async for _ in deadline_reader(Iterator(),60):pass
    async def caller():
        if handled:
            try:raise ValueError('already-handled')
            except ValueError:await consume()
        else:await consume()
    pending=asyncio.create_task(caller())
    try:
        await asyncio.wait_for(closing.wait(),1)
        pending.cancel();await asyncio.sleep(.01)
        assert not pending.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):await asyncio.wait_for(pending,1)
    assert calls==['close']
