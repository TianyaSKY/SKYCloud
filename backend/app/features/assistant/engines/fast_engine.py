"""Adapter from the existing SKYCloud RAG SSE generator to AssistantEvent."""

import json
from collections.abc import AsyncIterator, Sequence
from collections.abc import Callable
from typing import Any

from app.features.assistant.event_protocol import AssistantEvent
from app.features.chat.service import generate_chat_events


def _decode_legacy_sse(chunk: str | bytes) -> list[dict[str, Any]]:
    """Decode one or more legacy ``data:`` records.

    The compatibility parser is intentionally kept at the engine boundary so
    the new API exposes only the stable AssistantEvent contract.
    """

    if isinstance(chunk, bytes):
        chunk = chunk.decode("utf-8", errors="replace")
    events: list[dict[str, Any]] = []
    for line in str(chunk).splitlines():
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            # Keep non-JSON provider output out of the browser contract.
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events


class FastEngine:
    """Run the existing multi-query/RRF/rerank RAG pipeline."""

    def __init__(self, legacy_generator: Callable | None = None) -> None:
        # Dependency injection keeps the legacy /api/chat tests and adapter
        # useful without coupling the new engine to the old router module.
        self.legacy_generator = legacy_generator or generate_chat_events

    async def stream(
        self,
        *,
        run_id: int,
        user_id: int,
        workspace_id: int,
        query: str,
        history: Sequence[dict[str, str]],
        controller: object | None = None,
    ) -> AsyncIterator[AssistantEvent]:
        del controller
        async for chunk in self.legacy_generator(
            user_id,
            workspace_id,
            query,
            list(history),
        ):
            for legacy in _decode_legacy_sse(chunk):
                event_type = str(legacy.get("type") or "status")
                content = legacy.get("content")
                payload = dict(legacy.get("payload") or {})
                if event_type == "sources" and isinstance(legacy.get("sources"), list):
                    payload["sources"] = legacy["sources"]
                if event_type == "usage":
                    for key in ("prompt_tokens", "completion_tokens", "total_tokens", "model_name"):
                        if key in legacy:
                            payload[key] = legacy[key]
                if content is not None:
                    if event_type in {"token", "keywords", "status"}:
                        payload["content"] = content
                    elif isinstance(content, dict):
                        payload.update(content)
                if event_type == "status" and isinstance(content, str) and content.startswith("出错了:"):
                    event_type = "error"
                    payload = {"message": content[4:].strip()}
                yield AssistantEvent(type=event_type, payload=payload, run_id=run_id)
