"""兼容旧版 RAG 对话路由。

新前端使用 ``/assistant``；这里保留旧的扁平 SSE 字段，同时通过
Assistant FastEngine 适配器执行同一条 RAG 管线。
"""

import json
from loguru import logger
from contextlib import aclosing

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse

from app.api.dependencies import get_current_user, get_current_workspace
from app.features.chat.schemas import ChatRequest
from app.features.chat.stream_authorization import authorized_events
from app.exceptions import AuthenticationError, PermissionDeniedError
from app.infra.extensions import get_session_factory
from app.features.chat.service import generate_chat_events
from app.features.assistant.engines.fast_engine import FastEngine
from app.models.workspace import Workspace

router = APIRouter(tags=["chat"])


@router.post("/chat", response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}}})
async def chat(
    payload: ChatRequest,
    current_user=Depends(get_current_user),
    workspace: Workspace = Depends(get_current_workspace),
    session_factory=Depends(get_session_factory),
):
    """按工作空间文件检索并流式返回答案（text/event-stream）。"""
    if not payload.query:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Query is required"
        )

    async def stream():
        engine = FastEngine(legacy_generator=generate_chat_events)
        iterator = engine.stream(
            run_id=0,
            user_id=current_user.id,
            workspace_id=workspace.id,
            query=payload.query,
            history=payload.history,
        )
        try:
            async with aclosing(authorized_events(
                iterator, session_factory, int(workspace.id), int(current_user.id),
                getattr(current_user, '_http_stream_credential', None),
            )) as events:
                async for event in events:
                    legacy_payload = {"type": event.type, **event.payload}
                    if event.type == "error":
                        legacy_payload = {
                            "type": "status",
                            "content": f"出错了: {event.payload.get('message', '请求失败')}",
                        }
                    yield f"data: {json.dumps(legacy_payload, ensure_ascii=False)}\n\n"
        except AuthenticationError:
            yield f"data: {json.dumps({'type': 'status', 'content': '登录状态已失效，对话已停止，请重新登录。'})}\n\n"
        except PermissionDeniedError:
            yield f"data: {json.dumps({'type': 'status', 'content': '工作空间访问权限已撤销，对话已停止。'})}\n\n"
        except Exception as exc:
            logger.error("Legacy chat stream failed (error_type={})", type(exc).__name__)
            yield f"data: {json.dumps({'type': 'status', 'content': '对话请求失败，请稍后重试。'})}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")
