"""Common engine protocol and the per-run request contract."""

from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass
from typing import Protocol

from app.features.assistant.event_protocol import AssistantEvent
from app.features.assistant.run_control import RunController


@dataclass(frozen=True, slots=True, kw_only=True)
class EngineRequest:
    """Inputs shared by every assistant engine for one prepared run."""

    run_id: int
    user_id: int
    workspace_id: int
    query: str
    history: Sequence[dict[str, str]]
    controller: RunController | None = None


class AssistantEngine(Protocol):
    """Streaming assistant engine contract."""

    def stream(self, request: EngineRequest) -> AsyncGenerator[AssistantEvent, None]:
        """Yield ordered assistant events until the run settles."""
        ...
