"""在静默等待期间在 SQL worker 上检查助手授权测试."""
import asyncio
from collections.abc import AsyncGenerator
from contextlib import aclosing
import threading
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.exceptions import AuthenticationError
from app.features.assistant import service
from app.features.assistant.engines.base import EngineRequest
from app.features.assistant.event_protocol import AssistantEvent
from app.features.assistant.run_control import RunController
from app.features.assistant.stream_events import (
    HeartbeatOptions,
    engine_events_with_heartbeats,
)
from app.models import AssistantMessage, AssistantRun, User, Workspace


@pytest.mark.parametrize('phase', ['initial', 'event', 'quiet'])
async def test_credential_loss_cancels_durable_run_and_upstream(session: Session, test_user: User, test_workspace: Workspace, monkeypatch: pytest.MonkeyPatch, phase: str) -> None:
    conversation = service.create_conversation(session, test_workspace, test_user.id, 'fast', 'test')
    _, run, history = service.prepare_run(session, service.PrepareRunParams(
        workspace=test_workspace, user_id=test_user.id,
        conversation_id=conversation.id, query='question'))
    scope = (test_workspace.id, test_user.id, conversation.id, run.id)
    message_id = run.assistant_message_id
    revoked = [phase == 'initial']
    calls: list[str] = []
    received: list[AssistantEvent] = []
    proceed = asyncio.Event()
    loop_thread = threading.get_ident()
    class Credential:
        def validate(self, db: Session, user_id: int) -> None:
            assert threading.get_ident() != loop_thread and db is session and user_id == scope[1]
            calls.append('check')
            if revoked[0]:
                raise AuthenticationError('private-auth-error')
    class Engine:
        async def stream(self, request: EngineRequest) -> AsyncGenerator[AssistantEvent, None]:
            controller = request.controller
            assert controller is not None
            async def abort() -> None:
                calls.append('abort')
            controller.set_abort_callback(abort)
            try:
                yield AssistantEvent('token', {'content': 'allowed'})
                await proceed.wait()
                if phase == 'quiet':
                    await asyncio.Event().wait()
                yield AssistantEvent('token', {'content': 'private-late'})
            finally:
                calls.append('closed')
    monkeypatch.setattr(
        'app.features.assistant.engines.fast_engine.FastEngine', Engine)
    async def collect() -> None:
        params = service.StreamRunParams(
            session_factory=lambda: session,
            workspace_id=scope[0],
            user_id=scope[1],
            conversation_id=scope[2],
            run_id=scope[3],
            history=history,
            credential=Credential(), # type: ignore[arg-type] # pyright: ignore[reportArgumentType]
        )
        async for event in service.stream_run(params):
            received.append(event)
            if event.type == 'token':
                revoked[0] = True
                proceed.set()
    await asyncio.wait_for(collect(), 3)
    assert received[-1].type == 'cancelled'
    assert [e.payload['content'] for e in received if e.type == 'token'] == ([] if phase == 'initial' else ['allowed'])
    assert 'private-' not in str([e.payload for e in received])
    if phase != 'initial':
        assert 'abort' in calls and 'closed' in calls
    assert service.run_registry.get(scope[-1]) is None
    # SQL finish 早于所属 close; 使用 fixture Session 重新加载.
    run_stored = session.get(AssistantRun, scope[-1])
    assert run_stored is not None
    assert run_stored.status == 'cancelled'
    msg_stored = session.get(AssistantMessage, message_id)
    assert msg_stored is not None
    assert msg_stored.status == 'cancelled'


async def test_idle_checks_do_not_emit_early_heartbeats_and_drain_model() -> None:
    calls: list[bool] = []
    closed: list[bool] = []
    async def source() -> AsyncGenerator[AssistantEvent, None]:
        try:
            await asyncio.Event().wait()
            yield AssistantEvent('token', {})
        finally:
            closed.append(True)
    async def check() -> None:
        calls.append(True)
        if len(calls) == 3:
            raise AuthenticationError('revoked')
    emitted: list[AssistantEvent] = []
    with pytest.raises(AuthenticationError):
        async for event in engine_events_with_heartbeats(
            source(),
            1,
            HeartbeatOptions(
                interval_seconds=15,
                on_idle=check,
                poll_seconds=0.01,
            ),
        ):
            emitted.append(event)
    assert len(calls) == 3 and closed == [True] and not emitted


@pytest.mark.parametrize('cause', ['auth', 'close', 'cancel', 'abort-fault', 'abort-cancel'])
async def test_abort_precedes_provider_close_and_repeated_cancel_is_drained(monkeypatch: pytest.MonkeyPatch, cause: str) -> None:
    calls: list[str] = []
    entered, aborting, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = AuthenticationError('original authorization error')
    async def source() -> AsyncGenerator[AssistantEvent, None]:
        try:
            entered.set()
            if cause == 'close':
                yield AssistantEvent('token', {'content': 'allowed'})
            await asyncio.Event().wait()
            yield AssistantEvent('done', {})
        finally:
            assert 'abort' in calls
            calls.append('close')
    async def abort() -> None:
        calls.append('abort')
        aborting.set()
        if cause == 'cancel':
            await release.wait()
        if cause == 'abort-fault':
            raise RuntimeError('private-abort-url')
        if cause == 'abort-cancel':
            raise asyncio.CancelledError()
    async def idle() -> None:
        raise original
    logged: list[str] = []
    def mock_log(msg: str, *args: Any) -> None:
        logged.append(msg.format(*args))
    monkeypatch.setattr(service.logger, 'error', mock_log)
    async def collect() -> None:
        async with aclosing(engine_events_with_heartbeats(
            source(),
            1,
            HeartbeatOptions(
                on_idle=(
                    idle
                    if cause in ('auth', 'abort-fault', 'abort-cancel')
                    else None
                ),
                poll_seconds=0.01,
                on_abort=abort,
            ),
        )) as events:
            async for _event in events:
                if cause == 'close':
                    break
    pending = asyncio.create_task(collect())
    await asyncio.wait_for(entered.wait(), 1)
    if cause == 'cancel':
        pending.cancel()
        await asyncio.wait_for(aborting.wait(), 1)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done() and calls == ['abort']
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 1)
    elif cause == 'close':
        await asyncio.wait_for(pending, 1)
    else:
        with pytest.raises(AuthenticationError) as error:
            await asyncio.wait_for(pending, 1)
        assert error.value is original
    assert calls == ['abort', 'close']
    assert 'private-' not in str(logged)


@pytest.mark.parametrize('fails', [False, True])
async def test_controller_signals_engine_after_abort_rpc(fails: bool) -> None:
    controller = RunController()
    calls: list[str] = []
    async def engine() -> None:
        await controller.cancel_event.wait()
        calls.append('client-close')
    async def abort() -> None:
        await asyncio.sleep(0)
        assert not controller.cancel_event.is_set()
        calls.append('abort')
        if fails:
            raise RuntimeError('transport failure')
    controller.set_abort_callback(abort)
    waiting = asyncio.create_task(engine())
    await controller.request_cancel()
    await waiting
    assert calls == ['abort', 'client-close']


async def test_heartbeat_forwards_provider_failure_after_closing_source() -> None:
    calls: list[str] = []
    original = RuntimeError("provider failure")

    async def source() -> AsyncGenerator[AssistantEvent, None]:
        try:
            yield AssistantEvent("token", {"content": "accepted"})
            raise original
        finally:
            calls.append("close")

    async def abort() -> None:
        calls.append("abort")

    events = engine_events_with_heartbeats(
        source(), 1, HeartbeatOptions(on_abort=abort),
    )
    assert (await anext(events)).payload["content"] == "accepted"
    with pytest.raises(RuntimeError) as error:
        await anext(events)
    assert error.value is original
    assert calls == ["close", "abort"]

