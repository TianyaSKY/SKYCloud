"""Common engine protocol."""

from collections.abc import AsyncGenerator
from typing import Protocol, Sequence

from app.features.assistant.event_protocol import AssistantEvent


class AssistantEngine(Protocol):
    async def stream(
        self,
        *,
        run_id: int,
        user_id: int,
        workspace_id: int,
        query: str,
        history: Sequence[dict[str, str]],
        controller: object | None = None,
    ) -> AsyncGenerator[AssistantEvent, None]: ...
