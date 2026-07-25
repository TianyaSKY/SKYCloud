"""对话路由：RAG 问答 SSE 流式输出。业务在 chat_service。"""

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse

from app.api.dependencies import get_current_user, get_current_workspace
from app.features.chat.schemas import ChatRequest
from app.features.chat.service import generate_chat_events
from app.models.workspace import Workspace

router = APIRouter(tags=["chat"])


@router.post("/chat")
async def chat(
        payload: ChatRequest,
        current_user=Depends(get_current_user),
        workspace: Workspace = Depends(get_current_workspace),
):
    """按工作空间文件检索并流式返回答案（text/event-stream）。"""
    if not payload.query:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Query is required"
        )

    async def stream():
        async for chunk in generate_chat_events(
                current_user.id, workspace.id, payload.query, payload.history
        ):
            yield chunk

    return StreamingResponse(stream(), media_type="text/event-stream")
