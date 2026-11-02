"""通过助手事件契约运行现有 RAG 流水线."""

from collections.abc import AsyncGenerator
from contextlib import aclosing

from app.features.assistant.engines.base import EngineRequest
from app.features.assistant.event_protocol import AssistantEvent
from app.features.chat.service import generate_chat_events


class FastEngine:
    """运行现有的多查询/RRF/重排 RAG 流水线."""

    async def stream(
        self,
        request: EngineRequest,
    ) -> AsyncGenerator[AssistantEvent, None]:
        """产出附带当前运行标识的结构化 RAG 事件."""
        iterator = generate_chat_events(
            request.user_id,
            request.workspace_id,
            request.query,
            list(request.history),
        )
        async with aclosing(iterator):
            async for event in iterator:
                event.run_id = request.run_id
                yield event
