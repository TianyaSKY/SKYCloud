"""有界请求体在分发前同时受空闲与总摄入截止时间约束测试."""
import asyncio
import json
from typing import Any

import pytest
from starlette.types import Message, Scope

from runtime_controller import app as controller


@pytest.mark.parametrize('mode', ['drip', 'idle', 'late-final'])
async def test_body_total_or_idle_deadline_prevents_dispatch(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setattr(controller, 'MAX_BODY_SECONDS', 0.03, raising=False)
    monkeypatch.setattr(controller, 'MAX_BODY_IDLE_SECONDS', 0.01 if mode == 'idle' else 0.02, raising=False)
    now = [0.0]
    if mode == 'late-final':
        monkeypatch.setattr(controller, 'monotonic', lambda: now[0], raising=False)
    sent: list[Message] = []
    dispatched: list[bool] = []
    received = 0

    async def target(*args: object) -> None:
        dispatched.append(True)

    async def receive() -> Message:
        nonlocal received
        if mode == 'idle':
            await asyncio.sleep(0.05)
            return {'type': 'http.request', 'body': b'late', 'more_body': False}
        if mode == 'late-final':
            now[0] = 1.0
            return {'type': 'http.request', 'body': b'late', 'more_body': False}
        await asyncio.sleep(0.01)
        received += 1
        return {'type': 'http.request', 'body': b'x', 'more_body': received < 5}

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {'type': 'http', 'headers': [(b'x-runtime-controller-token', b'T' * 48)]}
    await asyncio.wait_for(controller.AuthAndBodyLimit(target, 'T' * 48)(scope, receive, send), 0.2)
    assert not dispatched
    assert sent[0]['status'] == 408
    body_bytes = sent[1].get('body', b'')
    assert isinstance(body_bytes, bytes)
    assert json.loads(body_bytes) == {'detail': 'Controller request timed out'}


async def test_fragmented_body_replayed_once_without_consuming_disconnect() -> None:
    messages: list[Message] = [
        {'type': 'http.request', 'body': b'a', 'more_body': True},
        {'type': 'http.request', 'body': b'b', 'more_body': False},
        {'type': 'http.disconnect'},
    ]
    seen: list[Message] = []

    async def receive() -> Message:
        return messages.pop(0)

    async def target(scope: Scope, receive: Any, send: Any) -> None:
        seen.append(await receive())
        seen.append(await receive())

    async def send(message: Message) -> None:
        pytest.fail('middleware must dispatch valid input')

    scope: Scope = {'type': 'http', 'headers': [(b'x-runtime-controller-token', b'T' * 48)]}
    await controller.AuthAndBodyLimit(target, 'T' * 48)(scope, receive, send)
    assert seen == [{'type': 'http.request', 'body': b'ab', 'more_body': False}, {'type': 'http.disconnect'}]
    assert not messages


async def test_body_cancellation_drains_receive_before_exit() -> None:
    entered, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls: list[str] = []

    async def receive() -> Message:
        entered.set()
        try:
            await asyncio.Event().wait()
            raise RuntimeError("unreachable")
        finally:
            closing.set()
            await release.wait()
            calls.append('drained')

    async def target(*args: object) -> None:
        pytest.fail('cancelled body must not dispatch')

    async def send(message: Message) -> None:
        pytest.fail('cancelled caller must not receive a timeout response')

    scope: Scope = {'type': 'http', 'headers': [(b'x-runtime-controller-token', b'T' * 48)]}
    operation = controller.AuthAndBodyLimit(target, 'T' * 48)(scope, receive, send)
    pending = asyncio.create_task(operation)
    try:
        await asyncio.wait_for(entered.wait(), 1)
        pending.cancel()
        await asyncio.wait_for(closing.wait(), 1)
        pending.cancel()
        await asyncio.sleep(0.01)
        assert not pending.done() and not calls
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 1)
    assert calls == ['drained']
