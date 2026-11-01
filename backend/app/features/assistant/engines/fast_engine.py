"""Run the existing RAG pipeline through the assistant event contract."""

from collections.abc import AsyncGenerator
from contextlib import aclosing

from app.features.assistant.engines.base import EngineRequest
from app.features.assistant.event_protocol import AssistantEvent
from app.features.chat.service import generate_chat_events


class FastEngine:
    """Run the existing multi-query/RRF/rerank RAG pipeline."""

    async def stream(
        self,
        request: EngineRequest,
    ) -> AsyncGenerator[AssistantEvent, None]:
        """Yield structured RAG events with the current run identity."""
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
