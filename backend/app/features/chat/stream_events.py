"""Translate RAG provider events and settle accepted usage when streams close.

The caller owns model-chain construction and supplies the event source and
usage writer. This module owns iterator closure and cancellation accounting.
"""
import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
import json
from typing import Any, Protocol

from loguru import logger
from app.infra.owned_work import run_owned
from app.features.chat.query_rewrite import require_keyword_dimensions, format_keyword_dimensions


class UsageRecorder(Protocol):
    def __call__(self, *, user_id: int, action: str, model_name: str | None,
                 prompt_tokens: int, completion_tokens: int, total_tokens: int,
                 query_summary: str) -> None: ...


async def stream_chat_events(
    event_factory: Callable[[], AsyncIterator[dict[str, Any]]], *,
    user_id: int, query: str, record_usage: UsageRecorder,
) -> AsyncIterator[str]:
    # ---------------------------------------------------------------------------
    # Token 用量追踪
    # ---------------------------------------------------------------------------
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
                kind = event["event"]

                # 仅在关键词链结束时推送完整关键词，避免中间态闪烁
                if kind == "on_chain_end" and event["name"] == "keyword_gen":
                    rewrite_output = event["data"].get("output")
                    dimensions = require_keyword_dimensions(rewrite_output)
                    keywords = format_keyword_dimensions(dimensions)
                    yield f"data: {json.dumps({'type': 'keywords', 'content': keywords})}\n\n"

                # 只转发 final_answer_model 的流，排除关键词生成阶段的 token
                elif kind == "on_chat_model_stream" and event["name"] == "final_answer_model":
                    content = event["data"]["chunk"].content
                    if content:
                        yield f"data: {json.dumps({'type': 'token', 'content': content})}\n\n"

                elif kind == "on_retriever_start" or (kind == "on_chain_start" and event["name"] == "custom_db_retriever"):
                    yield f"data: {json.dumps({'type': 'status', 'content': '正在检索相关文件...'})}\n\n"

                elif kind == "on_chain_end" and event["name"] == "custom_db_retriever":
                    # Expose only source metadata, never the complete retrieved
                    # chunks.  The new Assistant adapter persists this for the
                    # source panel while the legacy /api/chat client ignores it.
                    output = event.get("data", {}).get("output") or []
                    sources = []
                    if isinstance(output, list):
                        for doc in output:
                            metadata = getattr(doc, "metadata", {}) or {}
                            if not isinstance(metadata, dict):
                                continue
                            source = {
                                "file_id": metadata.get("id"),
                                "file_name": metadata.get("name"),
                                "page_number": metadata.get("page_number"),
                                "chunk_index": metadata.get("chunk_index"),
                            }
                            if source["file_id"] is not None:
                                sources.append(source)
                    if sources:
                        yield f"data: {json.dumps({'type': 'sources', 'sources': sources}, ensure_ascii=False)}\n\n"

                # 各 chat model 结束事件均可能带 usage_metadata，累加后统一落库
                elif kind == "on_chat_model_end":
                    output = event.get("data", {}).get("output")
                    if output and hasattr(output, "usage_metadata") and output.usage_metadata:
                        um = output.usage_metadata
                        usage_accumulator["prompt_tokens"] += um.get("input_tokens", 0)
                        usage_accumulator["completion_tokens"] += um.get("output_tokens", 0)
                        usage_accumulator["total_tokens"] += um.get("total_tokens", 0)
                    if not model_name_seen:
                        run_meta = event.get("metadata", {}) or {}
                        model_name_seen = run_meta.get("ls_model_name")

        if any(usage_accumulator.values()):
            yield f"data: {json.dumps({'type': 'usage', **usage_accumulator, 'model_name': model_name_seen})}\n\n"

    except Exception as e:
        logger.error("Chat request failed (error_type={})", type(e).__name__)
        yield f"data: {json.dumps({'type': 'error', 'payload': {'message': '对话请求失败，请稍后重试。'}})}\n\n"
    except BaseException:
        failed = True
        raise
    finally:
        # 成功或异常都记用量，避免漏计费
        try:
            await run_owned(
                record_usage, cancel_queued=False,
                user_id=user_id,
                action="chat",
                model_name=model_name_seen,
                prompt_tokens=usage_accumulator["prompt_tokens"],
                completion_tokens=usage_accumulator["completion_tokens"],
                total_tokens=usage_accumulator["total_tokens"],
                query_summary=query,
            )
        except asyncio.CancelledError:
            if not failed:
                raise
