"""静默上游读取必须遵循总响应截止时间, 随后排干所有权测试."""
import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import httpx
import pytest

from runtime_controller import app as controller
from runtime_controller.policy import Settings


@pytest.mark.parametrize('event', [False, True])
async def test_controller_quiet_response_reaches_deadline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, event: bool
) -> None:
    monkeypatch.setattr(controller, 'MAX_RESPONSE_SECONDS', 0.03, raising=False)
    monkeypatch.setattr(controller, 'MAX_EVENT_SECONDS', 0.03, raising=False)
    engine = Mock()
    engine.target.return_value = ('http://runtime.test', ('server', 'secret'))
    settings = Settings('T' * 48, 'S' * 48, 'test/runtime@sha256:' + 'a' * 64, 'test', '1g', 100000, 'test', str(tmp_path))
    closed: list[str] = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncGenerator[bytes, None]:
            try:
                yield b'data: allowed\n\n' if event else b'partial'
                await asyncio.Event().wait()
                raise RuntimeError("unreachable")
            finally:
                closed.append('reader')

        async def aclose(self) -> None:
            closed.append('response')

    actual_client = httpx.AsyncClient
    clients: list[httpx.AsyncClient] = []

    def client(**kwargs: Any) -> httpx.AsyncClient:
        response = httpx.Response(
            200,
            headers={'content-type': 'text/event-stream' if event else 'application/json'},
            stream=Stream(),
        )
        opened = actual_client(transport=httpx.MockTransport(lambda request: response), **kwargs)
        clients.append(opened)
        return opened

    monkeypatch.setattr(controller.httpx, 'AsyncClient', client)
    async with actual_client(
        transport=httpx.ASGITransport(app=controller.create_app(settings, engine)),
        base_url='http://controller.test',
    ) as downstream:
        result = await asyncio.wait_for(
            downstream.get(
                '/runtimes/' + 'a' * 64 + '/http/event',
                headers={'X-Runtime-Controller-Token': settings.token},
            ),
            0.3,
        )
    assert result.status_code == (200 if event else 502)
    if event:
        assert result.content == b'data: allowed\n\n'
    else:
        assert result.json() == {'detail': 'Runtime response timed out'}
    assert closed == ['reader', 'response']
    assert all(c.is_closed for c in clients)


@pytest.mark.parametrize('stage', ['ready', 'consumer'])
async def test_deadline_never_forwards_late_bytes(monkeypatch: pytest.MonkeyPatch, stage: str) -> None:
    from fastapi import HTTPException

    import runtime_common.http_stream as http_stream

    now = [0.0]
    monkeypatch.setattr(http_stream, 'monotonic', lambda: now[0])
    calls: list[str] = []

    class Iterator:
        def __aiter__(self) -> "Iterator":
            return self

        async def __anext__(self) -> bytes:
            calls.append('read')
            if stage == 'ready':
                now[0] = 2.0
            return b'private-late'

        async def aclose(self) -> None:
            calls.append('close')

    reader = http_stream.deadline_reader(Iterator(), 1)
    if stage == 'consumer':
        assert await anext(reader) == b'private-late'
        now[0] = 2.0
    with pytest.raises(HTTPException) as error:
        await anext(reader)
    assert error.value.status_code == 502
    assert calls == ['read', 'close']


@pytest.mark.parametrize('outcome', ['timeout', 'cancel'])
async def test_deadline_drains_reader_despite_repeated_cancellation(outcome: str) -> None:
    from fastapi import HTTPException

    from runtime_common.http_stream import deadline_reader

    entered, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls: list[str] = []

    class Iterator:
        def __aiter__(self) -> "Iterator":
            return self

        async def __anext__(self) -> bytes:
            entered.set()
            try:
                await asyncio.Event().wait()
                raise RuntimeError("unreachable")
            finally:
                closing.set()
                await release.wait()
                calls.append('drained')

        async def aclose(self) -> None:
            assert calls == ['drained']
            calls.append('close')

    pending = asyncio.create_task(anext(deadline_reader(Iterator(), 0.02 if outcome == 'timeout' else 60)))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if outcome == 'cancel':
            pending.cancel()
        await asyncio.wait_for(closing.wait(), 1)
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        await asyncio.sleep(0.01)
        assert not pending.done() and not calls
    finally:
        release.set()
        with pytest.raises(HTTPException if outcome == 'timeout' else asyncio.CancelledError) as error:
            await asyncio.wait_for(pending, 1)
        if outcome == 'timeout':
            assert isinstance(error.value, HTTPException)
            assert error.value.status_code == 502
    assert calls == ['drained', 'close']


@pytest.mark.parametrize('handled', [False, True])
async def test_deadline_eof_cleanup_preserves_new_cancellation(handled: bool) -> None:
    from runtime_common.http_stream import deadline_reader

    closing, release = asyncio.Event(), asyncio.Event()
    calls: list[str] = []

    class Iterator:
        def __aiter__(self) -> "Iterator":
            return self

        async def __anext__(self) -> bytes:
            raise StopAsyncIteration

        async def aclose(self) -> None:
            closing.set()
            await release.wait()
            calls.append('close')

    async def consume() -> None:
        async for _ in deadline_reader(Iterator(), 60):
            pass

    async def caller() -> None:
        if handled:
            try:
                raise ValueError('already-handled')
            except ValueError:
                await consume()
        else:
            await consume()

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
    assert calls == ['close']


@pytest.mark.parametrize('stage', ['ready', 'quiet'])
async def test_buffered_response_caller_disconnect_closes_owned_reader(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stage: str
) -> None:
    from starlette.requests import Request

    monkeypatch.setattr(controller, 'MAX_RESPONSE_SECONDS', 60)
    disconnected = False
    entered = asyncio.Event()
    closed: list[str] = []

    async def is_disconnected(self: Request) -> bool:
        return disconnected

    monkeypatch.setattr(Request, 'is_disconnected', is_disconnected)

    class Stream(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.iterator: AsyncGenerator[bytes, None] = self.read()

        def __aiter__(self) -> AsyncGenerator[bytes, None]:
            return self.iterator

        async def read(self) -> AsyncGenerator[bytes, None]:
            nonlocal disconnected
            try:
                yield b'partial'
                if stage == 'ready':
                    disconnected = True
                    yield b'private-late'
                else:
                    entered.set()
                    await asyncio.Event().wait()
            finally:
                closed.append('reader')

        async def aclose(self) -> None:
            await self.iterator.aclose()
            closed.append('response')

    settings = Settings('T' * 48, 'S' * 48, 'test/runtime@sha256:' + 'a' * 64, 'test', '1g', 100000, 'test', str(tmp_path))
    engine = Mock()
    engine.target.return_value = ('http://runtime.test', ('server', 'secret'))
    actual_client = httpx.AsyncClient
    clients: list[httpx.AsyncClient] = []

    def client(**kwargs: Any) -> httpx.AsyncClient:
        opened = actual_client(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Stream())), **kwargs)
        clients.append(opened)
        return opened

    monkeypatch.setattr(controller.httpx, 'AsyncClient', client)
    async with actual_client(
        transport=httpx.ASGITransport(app=controller.create_app(settings, engine)),
        base_url='http://controller.test',
    ) as downstream:
        pending = asyncio.create_task(
            downstream.get(
                '/runtimes/' + 'a' * 64 + '/http/session',
                headers={'X-Runtime-Controller-Token': settings.token},
            )
        )
        if stage == 'quiet':
            await asyncio.wait_for(entered.wait(), 1)
            disconnected = True
        result = await asyncio.wait_for(pending, 1.5)
    assert result.status_code == 499 and result.json() == {'detail': 'Runtime client disconnected'}
    assert closed == ['reader', 'response'] and all(c.is_closed for c in clients)
