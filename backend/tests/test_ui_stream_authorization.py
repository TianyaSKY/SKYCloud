"""UI chunks are authorized after reads; reader cleanup survives repeated cancel."""
import asyncio
import threading
import time
import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request
from app.features.workspace import ui_gateway,ui_tickets


@pytest.mark.parametrize('end',['chunk-revoke','quiet-revoke','repeat-cancel'])
async def test_ui_response_checks_current_authorization_and_drains_reader(monkeypatch,end):
    entered,cleaning,release,closed=(asyncio.Event() for _ in range(4))
    revoked=False
    calls,clients=[],[]
    loop_thread=threading.get_ident()
    def authorize(*args):
        assert threading.get_ident()!=loop_thread
        if revoked:raise HTTPException(403,'revoked')
        return {'expires_at':time.time()+60,'container_id':'a'*64,'origin':'http://runtime.test'}
    monkeypatch.setattr(ui_gateway,'authenticate_owned',authorize)
    monkeypatch.setattr(ui_gateway,'reauthenticate',authorize)
    monkeypatch.setattr(ui_gateway,'HTTP_RECHECK_SECONDS',0.01)
    class Stream(httpx.AsyncByteStream):
        def __init__(self):self.iterator=self.events()
        def __aiter__(self):return self.iterator
        async def events(self):
            nonlocal revoked
            try:
                yield b'allowed'
                if end=='chunk-revoke':
                    revoked=True;yield b'private-late'
                else:
                    entered.set();await asyncio.Event().wait()
            finally:
                if end=='repeat-cancel':cleaning.set();await release.wait()
                calls.append('reader');closed.set()
        async def aclose(self):
            await self.iterator.aclose()
            calls.append('transport')
    real_client=httpx.AsyncClient
    def upstream(**kwargs):
        client=real_client(transport=httpx.MockTransport(lambda request:httpx.Response(200,stream=Stream())),**kwargs)
        clients.append(client);return client
    monkeypatch.setattr(ui_gateway.httpx,'AsyncClient',upstream)
    messages=[{'type':'http.request','body':b'','more_body':False}]
    async def receive():
        if messages:return messages.pop()
        await asyncio.Event().wait()
    request=Request({'type':'http','method':'GET','path':'/runtime-ui/event','query_string':b'',
                     'headers':[(b'host',b'runtime.test'),(b'cookie',(ui_tickets.COOKIE+'=opaque').encode())]},receive)
    response=await ui_gateway.relay('event',request,None)
    iterator=response.body_iterator
    assert await anext(iterator)==b'allowed'
    pending=asyncio.create_task(anext(iterator))
    try:
        if end!='chunk-revoke':await asyncio.wait_for(entered.wait(),1)
        if end=='quiet-revoke':revoked=True
        if end=='repeat-cancel':
            pending.cancel();await asyncio.wait_for(cleaning.wait(),1)
            pending.cancel();await asyncio.sleep(0.01)
            assert not pending.done(),'active reader cleanup was interrupted'
        else:
            with pytest.raises(StopAsyncIteration):await asyncio.wait_for(pending,1.5)
    finally:
        release.set()
        if end=='repeat-cancel':
            with pytest.raises(asyncio.CancelledError):await asyncio.wait_for(pending,1)
        await iterator.aclose()
        await response.lease.close()
    await asyncio.wait_for(closed.wait(),1)
    assert calls==['reader','transport']
    assert len(clients)==1 and clients[0].is_closed
