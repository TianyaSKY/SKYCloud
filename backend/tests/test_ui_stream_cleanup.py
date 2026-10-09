"""Upstream leases close even when ASGI never enters the response body."""
import asyncio
import pytest
from starlette.requests import ClientDisconnect
from app.features.workspace import ui_stream


@pytest.mark.parametrize('phase',['header','body','cancel','normal'])
@pytest.mark.parametrize('fault',['none','response','client','both'])
async def test_delivery_outcome_and_both_resources_closed_once(monkeypatch,phase,fault):
    calls,logs=[],[]
    class Resource:
        def __init__(self,name):self.name=name
        async def aclose(self):
            calls.append(self.name)
            if fault in (self.name,'both'):raise RuntimeError('private-upstream-url')
    lease=ui_stream.UpstreamLease(Resource('client'))
    lease.upstream=Resource('response')
    monkeypatch.setattr(ui_stream.logger,'error',lambda msg,*args:logs.append(msg.format(*args)))
    closed=[]
    async def body():
        try:yield b'allowed'
        finally:
            closed.append(True)
            await lease.close()
    response=ui_stream.UpstreamResponse(body(),lease=lease)
    async def receive():await asyncio.Event().wait()
    async def send(message):
        if phase=='header' or (message['type']=='http.response.body' and phase!='normal'):
            if phase=='cancel':raise asyncio.CancelledError()
            raise OSError('connection lost')
    if phase=='normal':await response({'type':'http','asgi':{'spec_version':'2.4'}},receive,send)
    else:
        with pytest.raises(asyncio.CancelledError if phase=='cancel' else ClientDisconnect):
            await response({'type':'http','asgi':{'spec_version':'2.4'}},receive,send)
    assert calls==['response','client'] and closed==([] if phase=='header' else [True])
    assert 'private-' not in str(logs)


async def test_repeated_cancellation_drains_resource_close():
    entered,release=asyncio.Event(),asyncio.Event()
    calls=[]
    class Resource:
        async def aclose(self):
            entered.set()
            await release.wait()
            calls.append('closed')
    lease=ui_stream.UpstreamLease(Resource())
    pending=asyncio.create_task(lease.close())
    await entered.wait()
    pending.cancel();await asyncio.sleep(0)
    pending.cancel();await asyncio.sleep(0)
    assert not pending.done() and not calls
    release.set()
    with pytest.raises(asyncio.CancelledError):await pending
    await lease.close()
    assert calls==['closed']


@pytest.mark.parametrize('delivery',['headers','cancel','normal'])
@pytest.mark.parametrize('cleanup',['self-cancel','repeat-cancel'])
async def test_upstream_response_drains_iterator_before_resources(monkeypatch,delivery,cleanup):
    closing,release=asyncio.Event(),asyncio.Event()
    calls,logs=[],[]
    class Body:
        def __aiter__(self):return self
        async def __anext__(self):raise StopAsyncIteration
        async def aclose(self):
            closing.set()
            if cleanup=='repeat-cancel':await release.wait()
            calls.append('iterator')
            if cleanup=='self-cancel':raise asyncio.CancelledError('private-close-url')
    class Resource:
        def __init__(self,name):self.name=name
        async def aclose(self):
            assert calls[0]=='iterator'
            calls.append(self.name)
    lease=ui_stream.UpstreamLease(Resource('client'));lease.upstream=Resource('response')
    monkeypatch.setattr(ui_stream.logger,'error',lambda message,*args:logs.append(message.format(*args)))
    response=ui_stream.UpstreamResponse(Body(),lease=lease)
    async def receive():await asyncio.Event().wait()
    async def send(message):
        if delivery=='headers':raise OSError('header delivery failed')
        if delivery=='cancel':raise asyncio.CancelledError('delivery-cancel')
    pending=asyncio.create_task(response({'type':'http','asgi':{'spec_version':'2.4'}},receive,send))
    try:
        await asyncio.wait_for(closing.wait(),1)
        if cleanup=='repeat-cancel':
            pending.cancel();await asyncio.sleep(0)
            pending.cancel();await asyncio.sleep(0.01)
            assert not pending.done() and not calls
    finally:
        release.set()
        if delivery=='headers':
            with pytest.raises(ClientDisconnect):await asyncio.wait_for(pending,1)
        elif delivery=='cancel' or cleanup=='repeat-cancel':
            with pytest.raises(asyncio.CancelledError) as error:await asyncio.wait_for(pending,1)
            assert 'private-close-url' not in str(error.value)
        else:await asyncio.wait_for(pending,1)
    await lease.close()
    assert calls==['iterator','response','client']
    assert ('CancelledError' in str(logs)) is (cleanup=='self-cancel')
    assert 'private-close-url' not in str(logs)
