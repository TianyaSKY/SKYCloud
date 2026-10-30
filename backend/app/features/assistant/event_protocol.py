"""Internal assistant events and the HTTP SSE encoder.

Engines emit these stable events instead of leaking provider-specific payloads
to the browser.  This keeps the frontend compatible when the RAG pipeline or
OpenCode changes independently.
"""

import json
from dataclasses import dataclass, field
from typing import Any


def _empty_payload() -> dict[str, Any]:
    """Return a fresh event payload mapping."""
    return {}


@dataclass(slots=True)
class AssistantEvent:
    """One stable event emitted by an assistant engine."""

    type: str
    payload: dict[str, Any] = field(default_factory=_empty_payload)
    run_id: int | None = None
    message_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize this event for the browser SSE contract."""
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
    """Build the keep-alive event sent while the provider is idle."""
    return AssistantEvent(type="heartbeat", run_id=run_id)
