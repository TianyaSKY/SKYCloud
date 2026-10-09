"""UI admission waits revalidate on workers and drain abandoned operations."""
import asyncio
import threading
import time
import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request
from app.features.workspace import ui_gateway, ui_tickets


@pytest.mark.parametrize('stage,end',[('body','revoke'),('body','timeout'),('body','cancel'),('headers','revoke'),('headers','timeout'),('headers','cancel'),('headers','completed-revoked')])
async def test_ui_admission_wait_releases_resources(monkeypatch,stage,end):
    entered,body_closed=asyncio.Event(),asyncio.Event()
    revoked=False
    clients,responses=[],[]
    loop_thread=threading.get_ident()
    def authenticate(*args):
        assert threading.get_ident()!=loop_thread
        if revoked:raise HTTPException(403,'revoked')
        return {'expires_at':time.time()+60,'container_id':'a'*64,'origin':'http://runtime.test'}
    monkeypatch.setattr(ui_gateway,'authenticate_owned',authenticate)
    monkeypatch.setattr(ui_gateway,'reauthenticate',authenticate)
    monkeypatch.setattr(ui_gateway,'HTTP_RECHECK_SECONDS',0.01)
    monkeypatch.setattr(ui_gateway,'MAX_BODY_SECONDS',0.03 if end=='timeout' else 60)
    monkeypatch.setattr(ui_gateway,'MAX_HEADER_SECONDS',0.03 if end=='timeout' else 60)
    messages=[{'type':'http.request','body':b'partial','more_body':stage=='body'}]
    async def receive():
        waiting=False
        try:
            if messages:
                if stage=='body':entered.set()
                return messages.pop()
            waiting=True
            await asyncio.Event().wait()
        finally:
            if waiting:body_closed.set()
    request=Request({'type':'http','method':'POST','path':'/runtime-ui/event','query_string':b'',
                     'headers':[(b'host',b'runtime.test'),(b'origin',b'http://runtime.test'),
                                (b'cookie',(ui_tickets.COOKIE+'=opaque').encode())]},receive)
    actual_client=httpx.AsyncClient
    async def provider(request):
        nonlocal revoked
        entered.set()
        if end!='completed-revoked':await asyncio.Event().wait()
        revoked=True
        response=httpx.Response(200,stream=httpx.ByteStream(b'private-late'))
        assert not response.is_closed
        responses.append(response);return response
    def upstream(**kwargs):
        client=actual_client(transport=httpx.MockTransport(provider),**kwargs)
        clients.append(client);return client
    monkeypatch.setattr(ui_gateway.httpx,'AsyncClient',upstream)
    pending=asyncio.create_task(ui_gateway.relay('event',request,lambda:None))
    await asyncio.wait_for(entered.wait(),1)
    if end=='revoke':revoked=True
    if end=='cancel':
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):await asyncio.wait_for(pending,1)
    else:
        with pytest.raises(HTTPException) as error:await asyncio.wait_for(pending,1)
        assert error.value.status_code==((408 if stage=='body' else 504) if end=='timeout' else 403)
    assert len(clients)==(stage=='headers')
    assert all(client.is_closed for client in clients)
    assert all(response.is_closed for response in responses)
    if stage=='body':assert body_closed.is_set()
