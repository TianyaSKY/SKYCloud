"""公共引擎协议与单次运行请求契约."""

from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass
from typing import Protocol

from app.features.assistant.event_protocol import AssistantEvent
from app.features.assistant.run_control import RunController


@dataclass(frozen=True, slots=True, kw_only=True)
class EngineRequest:
    """单次准备好的运行中所有助手引擎共享的输入."""

    run_id: int
    user_id: int
    workspace_id: int
    query: str
    history: Sequence[dict[str, str]]
    controller: RunController | None = None


class AssistantEngine(Protocol):
    """流式助手引擎契约."""

    def stream(self, request: EngineRequest) -> AsyncGenerator[AssistantEvent, None]:
        """产出有序的助手事件, 直至运行结算."""
        ...
