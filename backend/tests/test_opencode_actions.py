# pyright: reportPrivateUsage=false
"""权限/中止控制操作需要显式上游确认测试."""
import asyncio
from typing import Any
import httpx
import pytest
from app.features.assistant.clients.opencode_client import OpenCodeClient, OpenCodeActionError


@pytest.mark.parametrize('action', ['permission', 'abort'])
@pytest.mark.parametrize('body', [None, False, {}, {'success': False}, {'success': 'true'}, [], 'ok', 1])
def test_unconfirmed_control_action_raises(action: str, body: Any) -> None:
    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    async def check() -> None:
        async with OpenCodeClient('http://runtime', 'user', 'password') as client:
            await client._client.aclose()
            client._client = httpx.AsyncClient(base_url='http://runtime', transport=httpx.MockTransport(handle))
            with pytest.raises(OpenCodeActionError):
                if action == 'permission':
                    await client.respond_permission('session', 'permission', 'once')
                else:
                    await client.abort_session('session')
    asyncio.run(check())


@pytest.mark.parametrize('status,body', [(204, None), (200, True), (200, {'success': True})])
def test_explicit_acknowledgment_is_accepted(status: int, body: Any) -> None:
    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    async def check() -> None:
        async with OpenCodeClient('http://runtime', 'user', 'password') as client:
            await client._client.aclose()
            client._client = httpx.AsyncClient(base_url='http://runtime', transport=httpx.MockTransport(handle))
            assert await client.respond_permission('session', 'permission', 'reject')
            assert await client.abort_session('session')
    asyncio.run(check())


def test_http_refusal_is_control_error_with_status_and_private_body() -> None:
    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text='private')

    async def check() -> None:
        async with OpenCodeClient('http://runtime', 'user', 'password') as client:
            await client._client.aclose()
            client._client = httpx.AsyncClient(base_url='http://runtime', transport=httpx.MockTransport(handle))
            with pytest.raises(OpenCodeActionError) as caught:
                await client.respond_permission('session', 'permission', 'once')
            assert caught.value.status_code == 403 and 'private' not in str(caught.value)
    asyncio.run(check())
