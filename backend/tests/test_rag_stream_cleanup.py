"""流式取消在返回前必须关闭嵌套生成器测试."""
import asyncio
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
from langchain_core.documents import Document
from langchain_core.messages import AIMessage, AIMessageChunk

from app.features.assistant.engines.base import EngineRequest
from app.features.assistant.engines.fast_engine import FastEngine
from app.features.assistant.event_protocol import AssistantEvent
from app.features.assistant.stream_events import engine_events_with_heartbeats

from app.features.chat.query_rewrite import RewriteKeywordDimensions
from app.features.chat.stream_events import stream_chat_events
from app.features.chat import service as rag


async def test_rag_stream_translates_provider_events_to_structured_payloads():
    dimensions = RewriteKeywordDimensions(
        topic_terms=["contract"],
        entity_terms=[],
        time_terms=[],
        file_type_terms=[],
        action_terms=[],
        synonym_terms=[],
    )
    provider_events = [
        {
            "event": "on_chain_end",
            "name": "keyword_gen",
            "data": {"output": dimensions},
            "metadata": {},
        },
        {
            "event": "on_retriever_start",
            "name": "custom_db_retriever",
            "data": {},
            "metadata": {},
        },
        {
            "event": "on_chain_end",
            "name": "custom_db_retriever",
            "data": {
                "output": [
                    Document(
                        page_content="private chunk",
                        metadata={
                            "id": 42,
                            "name": "policy.pdf",
                            "page_number": 3,
                            "chunk_index": 7,
                        },
                    )
                ]
            },
            "metadata": {},
        },
        {
            "event": "on_chat_model_stream",
            "name": "final_answer_model",
            "data": {"chunk": AIMessageChunk(content="answer")},
            "metadata": {},
        },
        {
            "event": "on_chat_model_end",
            "name": "final_answer_model",
            "data": {
                "output": AIMessage(
                    content="",
                    usage_metadata={
                        "input_tokens": 4,
                        "output_tokens": 2,
                        "total_tokens": 6,
                    },
                )
            },
            "metadata": {"ls_model_name": "structured-model"},
        },
    ]

    async def source():
        for event in provider_events:
            yield event

    recorded = []
    events = [
        event
        async for event in stream_chat_events(
            source,
            user_id=5,
            query="question",
            record_usage=recorded.append,
        )
    ]

    assert [event.type for event in events] == [
        "keywords",
        "status",
        "sources",
        "token",
        "usage",
    ]
    assert events[0].payload == {"content": "主题: contract"}
    assert events[1].payload == {"content": "正在检索相关文件..."}
    assert events[2].payload == {
        "sources": [
            {
                "file_id": 42,
                "file_name": "policy.pdf",
                "page_number": 3,
                "chunk_index": 7,
            }
        ]
    }
    assert events[3].payload == {"content": "answer"}
    assert events[4].payload == {
        "prompt_tokens": 4,
        "completion_tokens": 2,
        "total_tokens": 6,
        "model_name": "structured-model",
    }
    assert len(recorded) == 1
    assert recorded[0].user_id == 5
    assert recorded[0].model_name == "structured-model"
    assert recorded[0].prompt_tokens == 4
    assert recorded[0].completion_tokens == 2
    assert recorded[0].total_tokens == 6




@pytest.mark.parametrize("cancel_waiting", [False, True])
async def test_fast_engine_closes_structured_stream_before_returning(monkeypatch, cancel_waiting):
    closed = asyncio.Event()
    waiting = asyncio.Event()

    async def source(*args):
        try:
            yield AssistantEvent("token", {"content": "answer"})
            waiting.set()
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            closed.set()

    # 保留嵌套迭代器, 避免垃圾回收/终结化偶然满足断言.
    inner = source()
    monkeypatch.setattr(
        "app.features.assistant.engines.fast_engine.generate_chat_events",
        lambda *args: inner,
    )
    outer = FastEngine().stream(
        EngineRequest(
            run_id=1,
            user_id=2,
            workspace_id=3,
            query="hello",
            history=[],
        ),
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



@pytest.mark.parametrize("cancel_waiting", [False, True])
async def test_rag_close_awaits_model_cleanup_and_known_usage(monkeypatch, cancel_waiting):
    closed = asyncio.Event()
    waiting = asyncio.Event()
    recorded = []

    async def source():
        try:
            yield {"event": "on_chat_model_end", "name": "model", "data": {
                "output": AIMessage(content="", usage_metadata={"input_tokens": 2, "output_tokens": 3, "total_tokens": 5})},
                "metadata": {"ls_model_name": "synthetic"}}
            yield {"event": "on_chat_model_stream", "name": "final_answer_model", "data": {
                "chunk": AIMessageChunk(content="answer")}}
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
        event = await anext(outer)
        assert event.type == "token" and event.payload["content"] == "answer"
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
        assert recorded[0]["usage"].user_id == 2
        assert recorded[0]["usage"].total_tokens == 5
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
    outer = engine_events_with_heartbeats(inner, 1)
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
from app.features.assistant.engines.base import EngineRequest
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
    iterator = FastEngine().stream(EngineRequest(
        run_id=1, user_id=2, workspace_id=3, query='hello', history=[]))
    try:
        async for event in iterator:
            if event.type == 'token':
                break
        assert not closed.is_set(), 'Synthetic model already completed before cancellation'
        await iterator.aclose()
        assert closed.is_set(), 'Real LangChain model task survived outer stream close'
        assert len(recorded) == 1 and recorded[0]['usage'].user_id == 2
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
