"""Permission/abort control actions require explicit upstream acknowledgment."""
import asyncio
import httpx
import pytest
from app.features.assistant.clients.opencode_client import OpenCodeClient, OpenCodeActionError


@pytest.mark.parametrize('action',['permission','abort'])
@pytest.mark.parametrize('body',[None,False,{}, {'success':False}, {'success':'true'}, [], 'ok',1])
def test_unconfirmed_control_action_raises(action,body):
    async def check():
        async with OpenCodeClient('http://runtime','user','password') as client:
            await client._client.aclose()
            client._client=httpx.AsyncClient(base_url='http://runtime',transport=httpx.MockTransport(lambda request:httpx.Response(200,json=body)))
            with pytest.raises(OpenCodeActionError):
                if action=='permission': await client.respond_permission('session','permission','once')
                else: await client.abort_session('session')
    asyncio.run(check())


@pytest.mark.parametrize('status,body',[(204,None),(200,True),(200,{'success':True})])
def test_explicit_acknowledgment_is_accepted(status,body):
    async def check():
        async with OpenCodeClient('http://runtime','user','password') as client:
            await client._client.aclose()
            client._client=httpx.AsyncClient(base_url='http://runtime',transport=httpx.MockTransport(lambda request:httpx.Response(status,json=body)))
            assert await client.respond_permission('session','permission','reject')
            assert await client.abort_session('session')
    asyncio.run(check())


def test_http_refusal_is_control_error_with_status_and_private_body():
    async def check():
        async with OpenCodeClient('http://runtime','user','password') as client:
            await client._client.aclose()
            client._client=httpx.AsyncClient(base_url='http://runtime',transport=httpx.MockTransport(lambda request:httpx.Response(403,text='private')))
            with pytest.raises(OpenCodeActionError) as caught: await client.respond_permission('session','permission','once')
            assert caught.value.status_code==403 and 'private' not in str(caught.value)
    asyncio.run(check())
