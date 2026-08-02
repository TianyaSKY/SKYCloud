"""兼容旧版 RAG 对话路由。

新前端使用 ``/assistant``；这里保留旧的扁平 SSE 字段，同时通过
Assistant FastEngine 适配器执行同一条 RAG 管线。
"""

import json

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse

from app.api.dependencies import get_current_user, get_current_workspace
from app.features.chat.schemas import ChatRequest
from app.features.chat.service import generate_chat_events
from app.features.assistant.engines.fast_engine import FastEngine
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
        engine = FastEngine(legacy_generator=generate_chat_events)
        async for event in engine.stream(
            run_id=0,
            user_id=current_user.id,
            workspace_id=workspace.id,
            query=payload.query,
            history=payload.history,
        ):
            legacy_payload = {"type": event.type, **event.payload}
            if event.type == "error":
                legacy_payload = {
                    "type": "status",
                    "content": f"出错了: {event.payload.get('message', '请求失败')}",
                }
            yield f"data: {json.dumps(legacy_payload, ensure_ascii=False)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")
