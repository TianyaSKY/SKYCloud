"""Internal assistant events and the HTTP SSE encoder.

Engines emit these stable events instead of leaking provider-specific payloads
to the browser.  This keeps the frontend compatible when the RAG pipeline or
OpenCode changes independently.
"""

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class AssistantEvent:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    run_id: int | None = None
    message_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "type": self.type,
            "payload": self.payload,
        }
        if self.run_id is not None:
            result["run_id"] = self.run_id
        if self.message_id is not None:
            result["message_id"] = self.message_id
        return result


def encode_sse(event: AssistantEvent) -> str:
    """Encode one internal event as a compact, browser-safe SSE record."""

    return f"data: {json.dumps(event.to_dict(), ensure_ascii=False)}\n\n"


def heartbeat_event(run_id: int | None = None) -> AssistantEvent:
    return AssistantEvent(type="heartbeat", run_id=run_id)

