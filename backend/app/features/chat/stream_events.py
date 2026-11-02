"""将 RAG 供应商事件转换为稳定事件, 并在流关闭时结算用量.

调用方管理模型链构建并提供事件源与用量记录器。
本模块负责迭代器的妥善关闭以及取消时的用量记账。
"""

import asyncio
from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing
from typing import Protocol, cast

from langchain_core.documents import Document
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.runnables.schema import StreamEvent
from loguru import logger

from app.features.assistant.event_protocol import AssistantEvent
from app.features.chat.query_rewrite import (
    format_keyword_dimensions,
    require_keyword_dimensions,
)
from app.infra.llm.usage import TokenUsage
from app.infra.owned_work import run_owned_persistent


class UsageRecorder(Protocol):
    """持久化单条已确认的聊天 Token 用量记录."""

    def __call__(self, usage: TokenUsage) -> None:
        """记录单次已结算聊天流的 Token 用量."""


def _event_data(event: StreamEvent) -> dict[str, object]:
    """读取单次事件的数据字典映射, 不假定其为纯 JSON 值."""
    data: object = event["data"]
    if not isinstance(data, dict):
        return {}
    return cast("dict[str, object]", data)


def _chunk_content(chunk: object) -> object:
    """从最终回答的 SDK 数据块中读取流式生成的内容文本."""
    return chunk.content if isinstance(chunk, AIMessageChunk) else None


def _usage_counts(output: object) -> tuple[int, int, int] | None:
    """从单个模型结果中读取提示词、补全与总 Token 计数."""
    if not isinstance(output, AIMessage) or not output.usage_metadata:
        return None
    metadata = output.usage_metadata
    return (
        metadata.get("input_tokens", 0),
        metadata.get("output_tokens", 0),
        metadata.get("total_tokens", 0),
    )


def _model_name(event: StreamEvent) -> str | None:
    """从单次事件的运行元数据中读取声明的模型名称."""
    value: object = (event.get("metadata") or {}).get("ls_model_name")
    return value if isinstance(value, str) else None


def _keyword_event(event: StreamEvent) -> AssistantEvent | None:
    """在改写器完成处理后发送完整的关键词维度事件."""
    if event["event"] != "on_chain_end" or event["name"] != "keyword_gen":
        return None
    dimensions = require_keyword_dimensions(_event_data(event).get("output"))
    return AssistantEvent(
        type="keywords",
        payload={"content": format_keyword_dimensions(dimensions)},
    )


def _token_event(event: StreamEvent) -> AssistantEvent | None:
    """转发单个最终回答模型块, 排除改写器的 Token 干扰."""
    if event["event"] != "on_chat_model_stream":
        return None
    if event["name"] != "final_answer_model":
        return None
    content = _chunk_content(_event_data(event).get("chunk"))
    if not content:
        return None
    return AssistantEvent(type="token", payload={"content": content})


def _retrieval_start_event(event: StreamEvent) -> AssistantEvent | None:
    """在检索器启动时发出检索状态事件通知."""
    started = event["event"] == "on_retriever_start"
    chain_start = (
        event["event"] == "on_chain_start"
        and event["name"] == "custom_db_retriever"
    )
    if not (started or chain_start):
        return None
    return AssistantEvent(
        type="status",
        payload={"content": "正在检索相关文件..."},
    )


def _source_event(event: StreamEvent) -> AssistantEvent | None:
    """为来源展示面板公开来源元数据, 绝不暴露分块文本全文."""
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
    return AssistantEvent(type="sources", payload={"sources": sources})


def _accumulate_usage(event: StreamEvent, usage: dict[str, int]) -> None:
    """将单个模型结果的 Token 计数累加至当前累计统计中."""
    counts = _usage_counts(_event_data(event).get("output"))
    if counts is None:
        return
    prompt_tokens, completion_tokens, total_tokens = counts
    usage["prompt_tokens"] += prompt_tokens
    usage["completion_tokens"] += completion_tokens
    usage["total_tokens"] += total_tokens


def _usage_event(
    usage: dict[str, int],
    model_name: str | None,
) -> AssistantEvent | None:
    """构建结算用量事件; 若未统计到任何 Token 则返回 None."""
    if not any(usage.values()):
        return None
    return AssistantEvent(
        type="usage",
        payload={**usage, "model_name": model_name},
    )


def _assistant_event(event: StreamEvent) -> AssistantEvent | None:
    """将单个供应商事件映射为至多一个结构化助手事件."""
    if event["event"] == "on_chat_model_end":
        return None
    return (
        _keyword_event(event)
        or _token_event(event)
        or _retrieval_start_event(event)
        or _source_event(event)
    )


async def stream_chat_events(
    event_factory: Callable[[], AsyncGenerator[StreamEvent, None]], *,
    user_id: int, query: str, record_usage: UsageRecorder,
) -> AsyncGenerator[AssistantEvent, None]:
    """将供应商事件翻译为稳定助手事件, 并在退出时结算用量."""
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
                assistant_event = _assistant_event(event)
                if assistant_event is not None:
                    yield assistant_event

        usage_event = _usage_event(usage_accumulator, model_name_seen)
        if usage_event is not None:
            yield usage_event

    except Exception as e:
        logger.error("Chat request failed (error_type={})", type(e).__name__)
        yield AssistantEvent(
            type="error",
            payload={"message": "对话请求失败, 请稍后重试。"},
        )
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
