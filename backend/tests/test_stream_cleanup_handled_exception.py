"""A caller's already-handled exception must not hide a fresh cleanup cancellation."""
import asyncio
import pytest
from app.features.assistant import stream_response
from app.features.workspace import ui_stream
from app.features.chat import stream_authorization
from app.features.runtime_llm import router


@pytest.mark.parametrize('kind',['prepared','upstream','model-reader','legacy-reader','ui-reader'])
async def test_cleanup_cancellation_ignores_handled_caller_exception(monkeypatch,kind):
    closing,release=asyncio.Event(),asyncio.Event()
    calls=[]
    class Iterator:
        def __aiter__(self):return self
        async def __anext__(self):raise StopAsyncIteration
        async def aclose(self):closing.set();await release.wait();calls.append('close')
    async def receive():await asyncio.Event().wait()
    async def send(message):pass
    async def check(*args):pass
    monkeypatch.setattr(router,'verify_connected',check)
    monkeypatch.setattr(stream_authorization,'require_membership',lambda *args:None)
    if kind=='prepared':
        monkeypatch.setattr(stream_response,'cleanup_unclaimed_run',lambda *args:calls.append('release'))
        response=stream_response.PreparedStreamResponse(Iterator(),session_factory=None,run_scope=(1,2,3,4))
    elif kind=='upstream':
        class Lease:
            async def close(self):calls.append('release')
        response=ui_stream.UpstreamResponse(Iterator(),lease=Lease())
    if kind=='ui-reader':
        import time
        import httpx
        from starlette.requests import Request
        from app.features.workspace import ui_gateway,ui_tickets
        def authorize(*args):
            return {'expires_at':time.time()+60,'container_id':'a'*64,'origin':'http://runtime.test'}
        monkeypatch.setattr(ui_gateway,'authenticate_owned',authorize)
        monkeypatch.setattr(ui_gateway,'reauthenticate',authorize)
        real_client=httpx.AsyncClient
        def upstream(**kwargs):
            def provider(request):
                result=httpx.Response(200,content=b'')
                result.aiter_bytes=lambda:Iterator()
                return result
            return real_client(transport=httpx.MockTransport(provider),**kwargs)
        monkeypatch.setattr(ui_gateway.httpx,'AsyncClient',upstream)
        messages=[{'type':'http.request','body':b'','more_body':False}]
        async def read_request():
            if messages:return messages.pop()
            await asyncio.Event().wait()
        request=Request({'type':'http','method':'GET','path':'/runtime-ui/event','query_string':b'',
                         'headers':[(b'host',b'runtime.test'),(b'cookie',(ui_tickets.COOKIE+'=opaque').encode())]},read_request)
        response=await ui_gateway.relay('event',request,None)
    async def caller():
        try:raise ValueError('already-handled-caller-error')
        except ValueError:
            if kind in ('prepared','upstream'):
                await response({'type':'http','asgi':{'spec_version':'2.4'}},receive,send)
            elif kind=='ui-reader':
                async for _ in response.body_iterator:pass
            elif kind=='model-reader':
                class Response:
                    def aiter_bytes(self):return Iterator()
                async for _ in router.authorized_bytes(Response(),None,None):pass
            else:
                async for _ in stream_authorization.authorized_events(Iterator(),None,2,1):pass
    pending=asyncio.create_task(caller())
    try:
        await asyncio.wait_for(closing.wait(),1)
        pending.cancel();await asyncio.sleep(0.01)
        assert not pending.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):await asyncio.wait_for(pending,1)
    assert calls==(['close','release'] if kind in ('prepared','upstream') else ['close'])
