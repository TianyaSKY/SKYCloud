"""模型认证会话归属于其依赖项 worker 测试."""
import asyncio
from collections.abc import AsyncGenerator
import threading
from typing import Any
import httpx
import pytest
from fastapi import Depends, FastAPI
from app.features.runtime_llm import router
from app.infra.extensions import get_session_factory


@pytest.mark.parametrize('stage', ['dependency', 'body', 'quota', 'admission', 'headers', 'json', 'sse'])
@pytest.mark.parametrize('fault', [False, True])
async def test_cancelled_model_request_drains_control_worker_before_cleanup(
    monkeypatch: pytest.MonkeyPatch, stage: str, fault: bool
) -> None:
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    closed: list["Owned"] = []
    clients: list[httpx.AsyncClient] = []
    validations = 0
    target = {'dependency': 1, 'body': 2, 'admission': 3, 'headers': 4, 'json': 5, 'sse': 5}.get(stage)
    credential = {'sub': '1', 'workspace_id': 2, 'runtime_id': 3, 'model': 'owned-model', 'quota': 10000}

    class Owned:
        owner: int = 0

        def __enter__(self) -> "Owned":
            self.owner = threading.get_ident()
            return self

        def __exit__(self, *args: object) -> None:
            assert threading.get_ident() == self.owner
            closed.append(self)

    def hold() -> None:
        entered.set()
        try:
            assert release.wait(5)
            if fault:
                raise RuntimeError('private-control-error')
        finally:
            finished.set()

    def validate(session: Owned, raw: str, *, touch: bool = True) -> dict[str, Any]:
        nonlocal validations
        assert threading.get_ident() == session.owner
        validations += 1
        if validations == target:
            hold()
        return credential

    def reserve(*args: object) -> None:
        if stage == 'quota':
            hold()

    class ProviderBody(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncGenerator[bytes, None]:
            yield b'{"choices":[]}'

    def provider() -> httpx.AsyncClient:
        client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=ProviderBody())))
        clients.append(client)
        return client

    monkeypatch.setattr(router.tokens, 'validate_token', validate)
    monkeypatch.setattr(router.quota, 'reserve', reserve)
    monkeypatch.setattr(router, 'upstream_client', provider)
    monkeypatch.setattr(router, 'get_chat_model_config', lambda: {'api': 'http://provider/v1', 'key': 'private-key'})
    app = FastAPI()
    app.include_router(router.router)
    app.dependency_overrides[get_session_factory] = lambda: Owned
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
        pending = asyncio.create_task(client.post('/v1/chat/completions', headers={'Authorization': 'Bearer opaque'},
            json={'model': 'owned-model', 'messages': [{'role': 'user', 'content': 'hello'}], 'stream': stage == 'sse'}))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            previous = len(closed)
            pending.cancel('caller-stop')
            await asyncio.sleep(.01)
            pending.cancel('repeat-stop')
            await asyncio.sleep(.01)
            assert not pending.done() and not finished.is_set()
            assert len(closed) == previous and all(not p.is_closed for p in clients)
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError) as error:
                await asyncio.wait_for(pending, 2)
            assert await asyncio.to_thread(finished.wait, 2)
    # Starlette 的 SSE 任务组可能会替换外部取消消息;
    # worker 与上游所有权断言适用于两种响应.
    if stage != 'sse':
        assert str(error.value) == 'caller-stop'
    assert len(closed) == validations
    assert all(p.is_closed for p in clients)


@pytest.mark.parametrize('allowed', [True, False])
async def test_model_credential_session_owner_cleanup_and_loop(
    monkeypatch: pytest.MonkeyPatch, allowed: bool
) -> None:
    loop_thread = threading.get_ident()
    entered, release = threading.Event(), threading.Event()
    opened: list["Owned"] = []
    closed: list["Owned"] = []

    class Owned:
        def __init__(self) -> None:
            self.thread = threading.get_ident()
            assert self.thread != loop_thread
            opened.append(self)

        def __enter__(self) -> "Owned":
            return self

        def __exit__(self, *args: object) -> None:
            assert threading.get_ident() == self.thread
            closed.append(self)

    def validate(session: Owned, raw: str) -> dict[str, Any] | None:
        assert threading.get_ident() == session.thread and raw == 'opaque'
        entered.set()
        assert release.wait(5)
        return {'sub': '1'} if allowed else None

    monkeypatch.setattr(router.tokens, 'validate_token', validate)
    app = FastAPI()
    app.dependency_overrides[get_session_factory] = lambda: Owned

    @app.get('/probe')
    async def probe(credential: Any = Depends(router.current_credential)) -> Any:
        assert closed == opened
        return credential['sub']

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
        pending = asyncio.create_task(client.get('/probe', headers={'Authorization': 'Bearer opaque'}))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            await asyncio.wait_for(asyncio.sleep(0), 1)
            assert not pending.done()
        finally:
            release.set()
        assert (await pending).status_code == (200 if allowed else 401)
    assert len(opened) == 1 and closed == opened
