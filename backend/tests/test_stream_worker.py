# pyright: reportPrivateUsage=false
"""取消操作必须在其所有者线程中于 Session 清理前排干 SQL 工作测试."""
import asyncio
import threading
from typing import Any, cast

import pytest

from app.features.assistant import service
from app.features.assistant.stream_persistence import StreamWorker


async def test_worker_serializes_and_drains_repeated_cancellation() -> None:
    entered, release = threading.Event(), threading.Event()
    calls: list[tuple[str, int]] = []

    def slow() -> None:
        calls.append(('start', threading.get_ident()))
        entered.set()
        assert release.wait(5)
        calls.append(('finish', threading.get_ident()))

    async with StreamWorker() as worker:
        operation = asyncio.create_task(worker.call(slow))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            operation.cancel()
            await asyncio.sleep(0)
            operation.cancel()
            await asyncio.sleep(0)
            assert not operation.done()
            cleanup = asyncio.create_task(worker.call(lambda: calls.append(('close', threading.get_ident()))))
            await asyncio.sleep(0)
            assert not cleanup.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
        await cleanup
    assert [action for action, _ in calls] == ['start', 'finish', 'close']
    assert len({thread for _, thread in calls}) == 1
    assert calls[0][1] != threading.get_ident()


async def test_cancel_during_stream_session_creation_closes_on_owner_thread() -> None:
    entered, release = threading.Event(), threading.Event()
    calls: list[str] = []

    class Session:
        def __init__(self) -> None:
            self.owner = threading.get_ident()
            calls.append('create')

        def get(self, model: Any, identifier: Any) -> None:
            assert threading.get_ident() == self.owner
            entered.set()
            assert release.wait(5)
            return None  # Lookup failure after cancellation must still close.

        def close(self) -> None:
            assert threading.get_ident() == self.owner
            calls.append('close')

    events = service.stream_run(service.StreamRunParams(
        session_factory=cast(Any, Session),
        workspace_id=1,
        user_id=2,
        conversation_id=3,
        run_id=4,
        history=[],
    ))
    operation = asyncio.create_task(anext(events))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        operation.cancel()
        await asyncio.sleep(0)
        assert not operation.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await operation
    await events.aclose()
    assert calls == ['create', 'close']


async def test_actual_stream_sql_runs_on_worker_while_loop_is_responsive(session: Any, test_user: Any, test_workspace: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from sqlalchemy import event
    from sqlalchemy.orm import sessionmaker
    from app.features.assistant import repository
    from app.features.assistant.event_protocol import AssistantEvent

    conversation = repository.create_conversation(
        session,
        repository.CreateConversationParams(
            workspace_id=test_workspace.id,
            user_id=test_user.id,
            mode='fast',
            title='Slow SQL',
        ),
    )
    session.commit()
    conversation, run, history = service.prepare_run(
        session,
        service.PrepareRunParams(
            workspace=test_workspace,
            user_id=test_user.id,
            conversation_id=conversation.id,
            query='query',
        ),
    )

    class Engine:
        async def stream(self, request: Any) -> Any:
            yield AssistantEvent('token', {'content': 'Answer'})

    monkeypatch.setattr(
        'app.features.assistant.engines.fast_engine.FastEngine', Engine)
    loop_thread = threading.get_ident()
    entered, release = threading.Event(), threading.Event()
    owners: set[int] = set()

    def record(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
        assert threading.get_ident() != loop_thread
        owners.add(threading.get_ident())
        if statement.startswith('UPDATE assistant_messages'):
            entered.set()
            assert release.wait(5)

    workspace_id = int(test_workspace.id)
    user_id = int(test_user.id)
    conversation_id = int(conversation.id)
    run_id = int(run.id)
    engine = session.get_bind()
    event.listen(engine, 'before_cursor_execute', record)
    factory = sessionmaker(bind=engine, join_transaction_mode='create_savepoint')

    async def collect() -> list[AssistantEvent]:
        return [
            item
            async for item in service.stream_run(
                service.StreamRunParams(
                    session_factory=factory,
                    workspace_id=workspace_id,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    run_id=run_id,
                    history=history,
                )
            )
        ]

    operation = asyncio.create_task(collect())
    try:
        assert await asyncio.to_thread(entered.wait, 2)

        async def probe() -> bool:
            assert not release.is_set()
            return True

        assert await asyncio.wait_for(probe(), 1)
        assert not operation.done()
    finally:
        release.set()
        try:
            events = await operation
        finally:
            event.remove(engine, 'before_cursor_execute', record)
    assert [item.type for item in events] == ['run_started', 'token', 'done']
    assert len(owners) == 1


def test_replayed_permission_preserves_response_and_finish_preserves_terminal(session: Any, test_user: Any, test_workspace: Any) -> None:
    from app.features.assistant import repository
    from app.features.assistant.stream_persistence import (
        FinalOutcome,
        StreamPersistence,
        StreamProgress,
    )

    conversation = repository.create_conversation(
        session,
        repository.CreateConversationParams(
            workspace_id=test_workspace.id,
            user_id=test_user.id,
            mode='expert',
            title='Permission',
        ),
    )
    user_message = repository.create_message(
        session,
        conversation,
        repository.CreateMessageParams(role='user', content='query'),
    )
    assistant = repository.create_message(
        session,
        conversation,
        repository.CreateMessageParams(
            role='assistant',
            content='',
            status='streaming',
        ),
    )
    run = repository.create_run(session, conversation, user_message, assistant)
    payload = {"permission_id": "p1", "title": "Execute"}
    repository.register_permission(session, run, payload)
    session.commit()
    persistence = StreamPersistence(
        session,
        test_workspace,
        test_user.id,
        conversation,
        run,
    )
    assert repository.answer_permission(session, run, "p1", "once")
    session.commit()
    assert persistence.checkpoint(
        StreamProgress(answer_parts=[""], metadata={}, usage={}),
        commit=True,
        permission_boundary=True,
    )
    session.refresh(run)
    permission = repository.permission_for_run(session, run.id, "p1")
    assert permission is not None
    assert permission.state == "answered" and permission.response == "once"
    assert run.status == "running"
    assert not repository.answer_permission(session, run, "p1", "reject")
    repository.update_run_status(session, run, "failed")
    assistant.content = "Existing final answer"
    assistant.status = "failed"
    session.commit()
    assert permission.state == "cancelled" and permission.response == "once"
    assert persistence.finish(FinalOutcome(
        status='completed',
        content='Late answer',
        metadata={},
        usage={},
        events=[],
    )) == 'failed'
    session.refresh(run)
    session.refresh(assistant)
    assert run.status == assistant.status == 'failed'
    assert assistant.content == 'Existing final answer'


async def test_http_stream_disconnect_closes_underlying_iterator(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace
    from unittest.mock import Mock
    from app.features.assistant import router as module
    from app.api.dependencies import RequestContext
    from app.features.assistant.schemas import AssistantMessageRequest
    from app.features.assistant.event_protocol import AssistantEvent

    conversation = SimpleNamespace(id=3, mode='fast')
    run = SimpleNamespace(id=4)
    closed: list[bool] = []
    def fake_get_conv(*_args: Any, **_kwargs: Any) -> Any:
        return conversation

    def fake_prepare(*_args: Any, **_kwargs: Any) -> tuple[Any, Any, list[Any]]:
        return (conversation, run, [])

    monkeypatch.setattr(service, 'get_user_conversation', fake_get_conv)
    monkeypatch.setattr(service, 'prepare_run', fake_prepare)

    async def events(*args: Any, **kwargs: Any) -> Any:
        try:
            yield AssistantEvent('run_started', {'status': 'running'}, run_id=4)
            await asyncio.Event().wait()
        finally:
            closed.append(True)

    monkeypatch.setattr(service, 'stream_run', events)
    response = await module.stream_assistant_message(
        3,
        AssistantMessageRequest(query='query'),
        cast(Any, SimpleNamespace(headers={})),
        RequestContext(
            user=cast(Any, SimpleNamespace(id=1, http_stream_credential=None)),
            session_factory=Mock,
        ),
        cast(Any, SimpleNamespace(id=2)),
    )
    assert 'run_started' in await anext(cast(Any, response).body_iterator)
    await cast(Any, response).body_iterator.aclose()
    assert closed == [True]
