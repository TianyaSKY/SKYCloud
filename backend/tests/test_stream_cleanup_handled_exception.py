# pyright: reportPrivateUsage=false
"""调用方已处理的异常不得掩盖新的清理取消测试."""
import asyncio
from typing import Any, cast
import pytest
from app.features.assistant import stream_response
import runtime_common.http_stream as ui_stream
from app.features.runtime_llm import router


@pytest.mark.parametrize('kind', ['prepared', 'upstream', 'model-reader', 'ui-reader'])
async def test_cleanup_cancellation_ignores_handled_caller_exception(monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    closing, release = asyncio.Event(), asyncio.Event()
    calls: list[str] = []

    class Iterator:
        def __aiter__(self) -> 'Iterator':
            return self

        async def __anext__(self) -> bytes:
            raise StopAsyncIteration

        async def aclose(self) -> None:
            closing.set()
            await release.wait()
            calls.append('close')

    async def receive() -> Any:
        await asyncio.Event().wait()

    async def send(message: Any) -> None:
        pass

    async def check(*args: Any) -> None:
        pass

    monkeypatch.setattr(router, 'verify_connected', check)

    response: Any = None
    if kind == 'prepared':
        def fake_cleanup(*_args: Any, **_kwargs: Any) -> None:
            calls.append('release')
        monkeypatch.setattr(stream_response, 'cleanup_unclaimed_run', fake_cleanup)
        response = stream_response.PreparedStreamResponse(cast(Any, Iterator()), session_factory=cast(Any, None), run_scope=(1, 2, 3, 4))
    elif kind == 'upstream':
        class Lease:
            async def close(self) -> None:
                calls.append('release')
        response = ui_stream.UpstreamResponse(cast(Any, Iterator()), lease=cast(Any, Lease()))
    elif kind == 'ui-reader':
        import time
        import httpx
        from starlette.requests import Request
        from app.features.workspace import ui_gateway, ui_tickets

        def authorize(*args: Any) -> dict[str, Any]:
            return {'expires_at': time.time() + 60, 'container_id': 'a' * 64, 'origin': 'http://runtime.test', 'user_id': 1}

        monkeypatch.setattr(ui_gateway, 'authenticate_owned', authorize)
        monkeypatch.setattr(ui_gateway, 'reauthenticate', authorize)
        real_client = httpx.AsyncClient

        def upstream(**kwargs: Any) -> httpx.AsyncClient:
            def provider(request: httpx.Request) -> httpx.Response:
                result = httpx.Response(200, content=b'')
                setattr(result, "aiter_bytes", lambda: Iterator())
                return result
            return real_client(transport=httpx.MockTransport(provider), **kwargs)

        monkeypatch.setattr(ui_gateway.httpx, 'AsyncClient', upstream)
        messages: list[dict[str, Any]] = [{'type': 'http.request', 'body': b'', 'more_body': False}]

        async def read_request() -> dict[str, Any]:
            if messages:
                return messages.pop()
            await asyncio.Event().wait()
            return {}

        request = Request({'type': 'http', 'method': 'GET', 'path': '/runtime-ui/event', 'query_string': b'',
                         'headers': [(b'host', b'runtime.test'), (b'cookie', (ui_tickets.COOKIE + '=opaque').encode())]}, read_request)
        response = await ui_gateway.relay(path='event', request=request, session_factory=cast(Any, None))

    async def caller() -> None:
        try:
            raise ValueError('already-handled-caller-error')
        except ValueError:
            if kind in ('prepared', 'upstream'):
                await response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send)
            elif kind == 'ui-reader':
                async for _ in response.body_iterator:
                    pass
            elif kind == 'model-reader':
                class Response:
                    headers: dict[str, str] = {}
                    def aiter_bytes(self) -> Iterator:
                        return Iterator()
                async for _ in router.authorized_bytes(cast(Any, Response()), cast(Any, None), cast(Any, None)):
                    pass

    pending = asyncio.create_task(caller())
    try:
        await asyncio.wait_for(closing.wait(), 1)
        pending.cancel()
        await asyncio.sleep(0.01)
        assert not pending.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 1)
    assert calls == (['close', 'release'] if kind in ('prepared', 'upstream') else ['close'])
