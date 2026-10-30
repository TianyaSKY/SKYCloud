"""Adapter from the existing SKYCloud RAG SSE generator to AssistantEvent."""

import json
from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing

from runtime_controller.json_types import JsonObject, JsonValue

from app.features.assistant.engines.base import EngineRequest
from app.features.assistant.event_protocol import AssistantEvent
from app.features.chat.service import generate_chat_events

LegacyGenerator = Callable[
    [int, int, str, list[dict[str, str]]],
    AsyncGenerator[str | bytes, None],
]


def _decode_legacy_sse(chunk: str | bytes) -> list[JsonObject]:
    """Decode one or more legacy ``data:`` records.

    The compatibility parser is intentionally kept at the engine boundary so
    the new API exposes only the stable AssistantEvent contract.
    """
    if isinstance(chunk, bytes):
        chunk = chunk.decode("utf-8", errors="replace")
    events: list[JsonObject] = []
    for line in chunk.splitlines():
        if not line.startswith("data:"):
            continue
        raw = line.removeprefix("data:").strip()
        if not raw:
            continue
        try:
            payload: JsonValue = json.loads(raw)
        except (TypeError, ValueError):
            # Keep non-JSON provider output out of the browser contract.
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events


def _legacy_payload(legacy: JsonObject, event_type: str) -> dict[str, object]:
    """Build the AssistantEvent payload from one legacy record."""
    content = legacy.get("content")
    raw_payload = legacy.get("payload")
    payload: dict[str, object] = (
        dict(raw_payload) if isinstance(raw_payload, dict) else {}
    )
    sources = legacy.get("sources")
    if event_type == "sources" and isinstance(sources, list):
        payload["sources"] = sources
    if event_type == "usage":
        for key in (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "model_name",
        ):
            if key in legacy:
                payload[key] = legacy[key]
    if content is not None:
        if event_type in {"token", "keywords", "status"}:
            payload["content"] = content
        elif isinstance(content, dict):
            payload.update(content)
    return payload


def _should_emit_error(event_type: str, payload: dict[str, object]) -> bool:
    """Map the legacy Chinese error prefix onto the stable error event."""
    return (
        event_type == "status"
        and isinstance(payload.get("content"), str)
        and str(payload["content"]).startswith("出错了:")
    )


class FastEngine:
    """Run the existing multi-query/RRF/rerank RAG pipeline."""

    def __init__(
        self,
        legacy_generator: LegacyGenerator | None = None,
    ) -> None:
        """Optionally replace the legacy generator for tests."""
        # Dependency injection keeps the legacy /api/chat tests and adapter
        # useful without coupling the new engine to the old router module.
        self.legacy_generator: LegacyGenerator = (
            legacy_generator or generate_chat_events
        )

    async def stream(
        self,
        request: EngineRequest,
    ) -> AsyncGenerator[AssistantEvent, None]:
        """Adapt legacy SSE frames into stable assistant events."""
        iterator = self.legacy_generator(
            request.user_id,
            request.workspace_id,
            request.query,
            list(request.history),
        )
        async with aclosing(iterator):
            async for chunk in iterator:
                for legacy in _decode_legacy_sse(chunk):
                    event_type = str(legacy.get("type") or "status")
                    payload = _legacy_payload(legacy, event_type)
                    if _should_emit_error(event_type, payload):
                        event_type = "error"
                        payload = {
                            "message": str(payload["content"])[4:].strip(),
                        }
                    yield AssistantEvent(
                        type=event_type,
                        payload=payload,
                        run_id=request.run_id,
                    )
