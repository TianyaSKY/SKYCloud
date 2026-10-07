"""Upstream diagnostics stay private; only confirmed absence permits recovery."""
import asyncio
import traceback
from contextlib import asynccontextmanager
import httpx
import pytest
from app.features.assistant.clients.opencode_client import OpenCodeClient, OpenCodeClientError


@asynccontextmanager
async def client_for(handle):
    async with OpenCodeClient('http://runtime','user','password') as client:
        await client._client.aclose()
        client._client=httpx.AsyncClient(base_url='http://runtime',transport=httpx.MockTransport(handle))
        yield client


@pytest.mark.parametrize('failure,stream',[('http',False),('http',True),('transport',False),('transport',True),('json',False)])
def test_private_upstream_diagnostics_do_not_escape(failure,stream):
    secret='private-provider-key-or-prompt'
    def handle(request):
        if failure=='transport': raise httpx.ConnectError(secret,request=request)
        if failure=='http': return httpx.Response(503,text=secret)
        return httpx.Response(200,text=secret)
    async def check():
        async with client_for(handle) as client:
            with pytest.raises(OpenCodeClientError) as caught:
                if stream: await anext(client.stream_events())
                else: await client.get_session('session')
            error=caught.value
            assert secret not in ''.join(traceback.format_exception(error))
            assert error.status_code == (503 if failure=='http' else None)
    asyncio.run(check())


@pytest.mark.parametrize('status',[401,403,404,409,500])
def test_only_session_404_confirms_absence(status):
    async def check():
        async with client_for(lambda request:httpx.Response(status,json={'detail':'private'})) as client:
            if status==404: assert await client.get_session('session') is None
            else:
                with pytest.raises(OpenCodeClientError) as error: await client.get_session('session')
                assert error.value.status_code==status
    asyncio.run(check())


@pytest.mark.parametrize('body',[None,[],{}, {'id':'other'}, {'id':42}])
def test_malformed_success_is_not_session_absence(body):
    async def check():
        async with client_for(lambda request:httpx.Response(200,json=body)) as client:
            with pytest.raises(OpenCodeClientError,match='session identity'):
                await client.get_session('session')
    asyncio.run(check())


@pytest.mark.parametrize('operation,body',[('health',{}),('health',{'healthy':'true'}),('create_session',{'id':42}),('get_messages',{}),('get_messages',['invalid']),('get_diff',{}),('get_mcp_status',[]),('add_mcp',None)])
def test_malformed_success_is_not_empty_or_healthy(operation,body):
    async def check():
        async with client_for(lambda request:httpx.Response(200,json=body)) as client:
            args={'get_messages':('session',),'get_diff':('session',),'add_mcp':('name',{})}.get(operation,())
            with pytest.raises(OpenCodeClientError): await getattr(client,operation)(*args)
    asyncio.run(check())
