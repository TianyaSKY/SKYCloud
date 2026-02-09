from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse

from app.dependencies import get_current_user
from app.schemas import ChatRequest
from app.services.chat_service import generate_chat_events

router = APIRouter(tags=["chat"])


@router.post("/chat")
async def chat(payload: ChatRequest, current_user=Depends(get_current_user)):
    if not payload.query:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Query is required"
        )

    async def stream():
        async for chunk in generate_chat_events(
                current_user.id, payload.query, payload.history
        ):
            yield chunk

    return StreamingResponse(stream(), media_type="text/event-stream")
