"""Cancellation must wait for chat SQL and accepted accounting work."""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import anyio
from langchain_core.messages import AIMessage

from app.features.chat import repository, service


@pytest.mark.parametrize('kind', ['single', 'multi'])
async def test_retrieval_cancellation_drains_every_started_session(monkeypatch, kind):
    count = 1 if kind == 'single' else 3
    entered = [threading.Event() for _ in range(count)]
    release = [threading.Event() for _ in range(count)]
    sessions = []
    session_lock = threading.Lock()
    loop_thread = threading.get_ident()

    class Owned:
        def __init__(self):
            self.thread = threading.get_ident()
            self.closed = False
            with session_lock:
                self.index = len(sessions)
                sessions.append(self)

        def execute(self, *args):
            assert threading.get_ident() == self.thread != loop_thread
            entered[self.index].set()
            assert release[self.index].wait(3)
            return SimpleNamespace(fetchall=lambda: [])

        def close(self):
            assert threading.get_ident() == self.thread
            self.closed = True

    monkeypatch.setattr(service, 'SessionLocal', Owned)
    monkeypatch.setattr(repository, 'assert_member_locked', lambda *args: None)
    monkeypatch.setattr(service, 'get_embeddings_model', lambda: SimpleNamespace(
        aembed_query=AsyncMock(return_value=[1.0]),
        aembed_documents=AsyncMock(return_value=[[1.0]] * count),
    ))
    monkeypatch.setattr(service, 'build_multi_queries', lambda *args, **kwargs: ['one', 'two', 'three'])
    if kind == 'single':
        operation = service.custom_db_retriever('query', 2, user_id=1)
    else:
        operation = service.multi_query_db_retriever('query', 2, object(), user_id=1)
    pending = asyncio.create_task(operation)
    try:
        for event in entered:
            assert await asyncio.to_thread(event.wait, 1)
        pending.cancel('caller-stop')
        await asyncio.sleep(.01)
        pending.cancel('repeat-stop')
        await asyncio.sleep(.01)
        assert not pending.done() and not any(session.closed for session in sessions)
        if count > 1:
            release[0].set()
            await asyncio.sleep(.03)
            assert not pending.done(), 'one completed worker must not abandon its siblings'
    finally:
        for event in release:
            event.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 1)
        for _ in range(100):
            if all(session.closed for session in sessions):
                break
            await asyncio.sleep(.001)
    assert len(sessions) == count and all(session.closed for session in sessions)




@pytest.mark.parametrize('mode', ['record', 'producer', 'anyio'])
async def test_chat_cancellation_drains_accepted_usage(monkeypatch, mode):
    from app.features.token_usage import service as usage
    entered, release = threading.Event(), threading.Event()
    receipts = []
    scopes = []
    producer_waiting = asyncio.Event()

    def record(params):
        entered.set()
        assert release.wait(3)
        receipts.append(params)

    class Chain:
        def __or__(self, other):
            return self

        __ror__ = __or__

        def with_config(self, *args, **kwargs):
            return self

        def with_structured_output(self, *args, **kwargs):
            return self

        async def astream_events(self, *args, **kwargs):
            yield {'event': 'on_chat_model_end', 'data': {'output': AIMessage(
                content="", usage_metadata={'input_tokens': 3, 'output_tokens': 2, 'total_tokens': 5})},
                'metadata': {'ls_model_name': 'synthetic-model'}}
            if mode == 'producer':
                producer_waiting.set()
                await asyncio.Event().wait()

    monkeypatch.setattr(usage, 'record_usage', record)
    monkeypatch.setattr(service, 'get_chat_model', Chain)
    monkeypatch.setattr(service, 'ChatPromptTemplate', SimpleNamespace(from_template=lambda *args: Chain()))
    monkeypatch.setattr(service, 'RunnableParallel', lambda *args: Chain())
    monkeypatch.setattr(service, 'RunnableLambda', lambda *args: Chain())
    monkeypatch.setattr(service, 'StrOutputParser', Chain)

    async def collect():
        with anyio.CancelScope() as scope:
            scopes.append(scope)
            return [part async for part in service.generate_chat_events(7, 2, 'query', [])]
        assert mode == 'anyio' and scope.cancelled_caught

    pending = asyncio.create_task(collect())
    try:
        if mode == 'producer':
            await asyncio.wait_for(producer_waiting.wait(), 1)
            pending.cancel('original-stop')
        assert await asyncio.to_thread(entered.wait, 1)
        if mode == 'anyio':
            scopes[0].cancel()
        else:
            pending.cancel('record-stop')
        await asyncio.sleep(.01)
        if mode != 'anyio':
            pending.cancel('repeat-stop')
        await asyncio.sleep(.01)
        assert not pending.done() and not receipts
    finally:
        release.set()
        if mode == 'anyio':
            await asyncio.wait_for(pending, 1)
        else:
            with pytest.raises(asyncio.CancelledError) as error:
                await asyncio.wait_for(pending, 1)
            assert str(error.value) == ('original-stop' if mode == 'producer' else 'record-stop')
        for _ in range(100):
            if receipts:
                break
            await asyncio.sleep(.001)
    assert len(receipts) == 1
    assert receipts[0].user_id == 7 and receipts[0].total_tokens == 5
