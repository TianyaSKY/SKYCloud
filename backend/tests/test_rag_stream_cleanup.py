"""Streaming cancellation must close nested generators before returning."""
import asyncio
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from app.features.assistant.engines.fast_engine import FastEngine
from app.features.assistant.event_protocol import AssistantEvent
from app.features.assistant.service import _engine_events_with_heartbeats
from app.features.chat import router as chat_router
from app.features.chat import service as rag


@pytest.mark.parametrize("cancel_waiting", [False, True])
async def test_fast_engine_closes_legacy_stream_before_returning(cancel_waiting):
    closed = asyncio.Event()
    waiting = asyncio.Event()

    async def source(*args):
        try:
            yield 'data: {"type":"token","content":"answer"}\n\n'
            waiting.set()
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            closed.set()

    # Retain the nested iterator so GC/finalization cannot satisfy the assertion.
    inner = source()
    outer = FastEngine(legacy_generator=lambda *args: inner).stream(
        run_id=1, user_id=2, workspace_id=3, query="hello", history=[],
    )
    assert (await anext(outer)).payload["content"] == "answer"
    try:
        if cancel_waiting:
            pending = asyncio.create_task(anext(outer))
            await asyncio.wait_for(waiting.wait(), 1)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        else:
            await outer.aclose()
        assert closed.is_set(), "Nested RAG generator is still open"
    finally:
        await outer.aclose()
        await inner.aclose()


async def test_legacy_http_stream_close_reaches_rag_generator(monkeypatch):
    closed = asyncio.Event()

    async def source(*args):
        try:
            yield 'data: {"type":"token","content":"answer"}\n\n'
            await asyncio.Event().wait()
        finally:
            closed.set()

    inner = source()
    monkeypatch.setattr(chat_router, "generate_chat_events", lambda *args: inner)
    response = await chat_router.chat(
        SimpleNamespace(query="hello", history=[]),
        SimpleNamespace(id=2), SimpleNamespace(id=3),
    )
    try:
        assert 'answer' in await anext(response.body_iterator)
        await response.body_iterator.aclose()
        assert closed.is_set()
    finally:
        await response.body_iterator.aclose()
        await inner.aclose()


@pytest.mark.parametrize("cancel_waiting", [False, True])
async def test_rag_close_awaits_model_cleanup_and_known_usage(monkeypatch, cancel_waiting):
    closed = asyncio.Event()
    waiting = asyncio.Event()
    recorded = []

    async def source():
        try:
            yield {"event": "on_chat_model_end", "name": "model", "data": {
                "output": SimpleNamespace(usage_metadata={"input_tokens": 2, "output_tokens": 3, "total_tokens": 5})},
                "metadata": {"ls_model_name": "synthetic"}}
            yield {"event": "on_chat_model_stream", "name": "final_answer_model", "data": {
                "chunk": SimpleNamespace(content="answer")}}
            waiting.set()
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            closed.set()

    inner = source()

    class Chain:
        def __or__(self, other):
            return self
        def __ror__(self, other):
            return self
        def with_config(self, *args, **kwargs):
            return self
        def with_structured_output(self, *args, **kwargs):
            return self
        def astream_events(self, *args, **kwargs):
            return inner

    chain = Chain()
    monkeypatch.setattr(rag, "get_chat_model", lambda: chain)
    monkeypatch.setattr(rag, "ChatPromptTemplate", SimpleNamespace(from_template=lambda *args: chain))
    monkeypatch.setattr(rag, "RunnableParallel", lambda *args: chain)
    monkeypatch.setattr(rag, "RunnableLambda", lambda *args: chain)
    monkeypatch.setattr("app.infra.llm.client.record_llm_usage", lambda **kwargs: recorded.append(kwargs))
    outer = rag.generate_chat_events(2, 3, "hello", [])
    try:
        assert 'answer' in await anext(outer)
        if cancel_waiting:
            pending = asyncio.create_task(anext(outer))
            await asyncio.wait_for(waiting.wait(), 1)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        else:
            await outer.aclose()
        assert closed.is_set()
        assert len(recorded) == 1
        assert recorded[0]["user_id"] == 2
        assert recorded[0]["total_tokens"] == 5
    finally:
        await outer.aclose()
        await inner.aclose()


async def test_slow_stream_has_backpressure_and_full_queue_cancel_finishes():
    pulled = 0
    closed = asyncio.Event()

    async def source():
        nonlocal pulled
        try:
            while True:
                pulled += 1
                yield AssistantEvent("token", {"content": str(pulled)}, run_id=1)
                await asyncio.sleep(0)
        finally:
            await asyncio.sleep(0)
            closed.set()

    inner = source()
    outer = _engine_events_with_heartbeats(inner, 1)
    try:
        assert (await anext(outer)).payload["content"] == "1"
        for _ in range(20):
            await asyncio.sleep(0)
        assert pulled <= 3, "Slow consumer allowed unbounded provider read-ahead"
        await asyncio.wait_for(outer.aclose(), 1)
        assert closed.is_set(), "Full queue cancellation left the producer open"
    finally:
        await outer.aclose()
        await inner.aclose()


REAL_CHAIN = r'''
import asyncio
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.runnables import RunnableLambda
from app.features.chat import service as rag
from app.features.chat.query_rewrite import RewriteKeywordDimensions
from app.features.assistant.engines.fast_engine import FastEngine
from app.infra.llm import client

closed = None
recorded = []

class TrackedModel(FakeListChatModel):
    async def _astream(self, *args, **kwargs):
        try:
            async for chunk in super()._astream(*args, **kwargs):
                yield chunk
        finally:
            await asyncio.sleep(0)
            closed.set()

class Model:
    def with_structured_output(self, *args, **kwargs):
        return RunnableLambda(lambda prompt: RewriteKeywordDimensions(
            topic_terms=[], entity_terms=[], time_terms=[], file_type_terms=[],
            action_terms=[], synonym_terms=[]))
    def with_config(self, config):
        return TrackedModel(responses=['synthetic answer'], sleep=.02).with_config(config)

class Embeddings:
    async def aembed_query(self, *args, **kwargs):
        return [.1] * 1024
    async def aembed_documents(self, texts, **kwargs):
        return [[.1] * 1024 for _ in texts]

rag.get_chat_model = lambda: Model()
rag.get_embeddings_model = lambda: Embeddings()
rag._db_search_by_vector = lambda *args: []
client.record_llm_usage = lambda **kwargs: recorded.append(kwargs)

async def main():
    global closed
    closed = asyncio.Event()
    iterator = FastEngine().stream(run_id=1, user_id=2, workspace_id=3,
                                   query='hello', history=[])
    try:
        async for event in iterator:
            if event.type == 'token':
                break
        assert not closed.is_set(), 'Synthetic model already completed before cancellation'
        await iterator.aclose()
        assert closed.is_set(), 'Real LangChain model task survived outer stream close'
        assert len(recorded) == 1 and recorded[0]['user_id'] == 2
    finally:
        await iterator.aclose()

asyncio.run(main())
print('REAL_RAG_CHAIN_CLOSED')
'''


def test_real_langchain_task_closes_with_fast_engine():
    result = subprocess.run([sys.executable, '-c', REAL_CHAIN],
        cwd=Path(__file__).resolve().parents[1], env=os.environ.copy(),
        capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'REAL_RAG_CHAIN_CLOSED' in result.stdout
