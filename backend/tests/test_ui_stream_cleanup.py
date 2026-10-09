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
