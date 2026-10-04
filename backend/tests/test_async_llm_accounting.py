"""Database accounting must leave the event loop available to other requests."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.infra.llm.client import chat_completion, embed_texts


@pytest.mark.parametrize("operation", ["chat", "embedding"])
async def test_slow_accounting_does_not_block_requests(operation):
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    response = SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=2, total_tokens=3),
        data=[SimpleNamespace(index=0, embedding=[0.1])],
    )
    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=response)
    client.embeddings.create = AsyncMock(return_value=response)

    def slow_record(**kwargs):
        loop.call_soon_threadsafe(started.set)
        release.wait(timeout=2)

    with patch("app.infra.llm.client._get_client", return_value=client), \
         patch("app.infra.llm.client._safe_record", side_effect=slow_record):
        config = {"api": "http://provider", "key": "key", "model": "model"}
        call = chat_completion(messages=[], config=config) if operation == "chat" else embed_texts(texts="hello", config=config)
        task = asyncio.create_task(call)
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            assert not task.done(), "Accounting blocked the event loop until the request finished"
        finally:
            release.set()
            await task
