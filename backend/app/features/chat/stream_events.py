"""Translate RAG provider events and settle accepted usage when streams close.

The caller owns model-chain construction and supplies the event source and
usage writer. This module owns iterator closure and cancellation accounting.
"""

import asyncio
import json
from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing
from typing import Protocol, cast

from langchain_core.documents import Document
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.runnables.schema import StreamEvent
from loguru import logger

from app.features.chat.query_rewrite import (
    format_keyword_dimensions,
    require_keyword_dimensions,
)
from app.infra.llm.usage import TokenUsage
from app.infra.owned_work import run_owned_persistent


class UsageRecorder(Protocol):
    """Persist one accepted chat usage record."""

    def __call__(self, usage: TokenUsage) -> None:
        """Record the usage of one settled chat stream."""


def _event_data(event: StreamEvent) -> dict[str, object]:
    """Read one event's data mapping without assuming pure JSON values."""
    data: object = event["data"]
    if not isinstance(data, dict):
        return {}
    return cast("dict[str, object]", data)


def _chunk_content(chunk: object) -> object:
    """Read streamed content from the final-answer SDK chunk."""
    return chunk.content if isinstance(chunk, AIMessageChunk) else None


def _usage_counts(output: object) -> tuple[int, int, int] | None:
    """Read prompt/completion/total token counts from one model result."""
    if not isinstance(output, AIMessage) or not output.usage_metadata:
        return None
    metadata = output.usage_metadata
    return (
        metadata.get("input_tokens", 0),
        metadata.get("output_tokens", 0),
        metadata.get("total_tokens", 0),
    )


def _model_name(event: StreamEvent) -> str | None:
    """Read the model name advertised in one event's run metadata."""
    value: object = (event.get("metadata") or {}).get("ls_model_name")
    return value if isinstance(value, str) else None


def _keyword_frame(event: StreamEvent) -> str | None:
    """Emit the complete keyword dimensions once the rewriter settles."""
    if event["event"] != "on_chain_end" or event["name"] != "keyword_gen":
        return None
    dimensions = require_keyword_dimensions(_event_data(event).get("output"))
    keywords = format_keyword_dimensions(dimensions)
    return f"data: {json.dumps({'type': 'keywords', 'content': keywords})}\n\n"


def _token_frame(event: StreamEvent) -> str | None:
    """Forward one final-answer model chunk, excluding rewriter tokens."""
    if event["event"] != "on_chat_model_stream":
        return None
    if event["name"] != "final_answer_model":
        return None
    content = _chunk_content(_event_data(event).get("chunk"))
    if not content:
        return None
    return f"data: {json.dumps({'type': 'token', 'content': content})}\n\n"


def _retrieval_start_frame(event: StreamEvent) -> str | None:
    """Emit the retrieval status frame when the retriever starts."""
    started = event["event"] == "on_retriever_start"
    chain_start = (
        event["event"] == "on_chain_start"
        and event["name"] == "custom_db_retriever"
    )
    if not (started or chain_start):
        return None
    content = "正在检索相关文件..."
    return f"data: {json.dumps({'type': 'status', 'content': content})}\n\n"


def _source_frame(event: StreamEvent) -> str | None:
    """Expose only source metadata for the sources panel, never chunk text."""
    if event["event"] != "on_chain_end" or event["name"] != "custom_db_retriever":
        return None
    output: object = _event_data(event).get("output") or []
    sources: list[dict[str, object]] = []
    if isinstance(output, list):
        documents = cast("list[object]", output)
        for doc in documents:
            if not isinstance(doc, Document):
                continue
            fields = doc.metadata
            source: dict[str, object] = {
                "file_id": fields.get("id"),
                "file_name": fields.get("name"),
                "page_number": fields.get("page_number"),
                "chunk_index": fields.get("chunk_index"),
            }
            if source["file_id"] is not None:
                sources.append(source)
    if not sources:
        return None
    payload: dict[str, object] = {"type": "sources", "sources": sources}
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _accumulate_usage(event: StreamEvent, usage: dict[str, int]) -> None:
    """Add one model result's token counts to the running totals."""
    counts = _usage_counts(_event_data(event).get("output"))
    if counts is None:
        return
    prompt_tokens, completion_tokens, total_tokens = counts
    usage["prompt_tokens"] += prompt_tokens
    usage["completion_tokens"] += completion_tokens
    usage["total_tokens"] += total_tokens


def _usage_frame(usage: dict[str, int], model_name: str | None) -> str | None:
    """Render the settled usage frame, or nothing when no tokens were seen."""
    if not any(usage.values()):
        return None
    payload: dict[str, object] = {"type": "usage", **usage, "model_name": model_name}
    return f"data: {json.dumps(payload)}\n\n"


def _event_frame(event: StreamEvent) -> str | None:
    """Map one provider event onto at most one legacy SSE frame."""
    if event["event"] == "on_chat_model_end":
        return None
    frame = _keyword_frame(event)
    if frame is None:
        frame = _token_frame(event)
    if frame is None:
        frame = _retrieval_start_frame(event)
    if frame is None:
        frame = _source_frame(event)
    return frame


async def stream_chat_events(
    event_factory: Callable[[], AsyncGenerator[StreamEvent, None]], *,
    user_id: int, query: str, record_usage: UsageRecorder,
) -> AsyncGenerator[str, None]:
    """Translate provider events into legacy SSE frames and settle usage."""
    usage_accumulator: dict[str, int] = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }
    model_name_seen: str | None = None

    failed = False
    try:
        iterator = event_factory()
        async with aclosing(iterator):
            async for event in iterator:
                # 各 chat model 结束事件均可能带 usage_metadata, 累加后统一落库
                if event["event"] == "on_chat_model_end":
                    _accumulate_usage(event, usage_accumulator)
                    if model_name_seen is None:
                        model_name_seen = _model_name(event)
                    continue
                frame = _event_frame(event)
                if frame is not None:
                    yield frame

        usage_frame = _usage_frame(usage_accumulator, model_name_seen)
        if usage_frame is not None:
            yield usage_frame

    except Exception as e:
        logger.error("Chat request failed (error_type={})", type(e).__name__)
        payload: dict[str, object] = {
            "type": "error",
            "payload": {"message": "对话请求失败, 请稍后重试。"},
        }
        yield f"data: {json.dumps(payload)}\n\n"
    except BaseException:
        failed = True
        raise
    finally:
        # 成功或异常都记用量, 避免漏计费
        usage = TokenUsage(
            user_id=user_id,
            action="chat",
            model_name=model_name_seen,
            prompt_tokens=usage_accumulator["prompt_tokens"],
            completion_tokens=usage_accumulator["completion_tokens"],
            total_tokens=usage_accumulator["total_tokens"],
            query_summary=query,
        )
        try:
            await run_owned_persistent(record_usage, usage)
        except asyncio.CancelledError:
            if not failed:
                raise
