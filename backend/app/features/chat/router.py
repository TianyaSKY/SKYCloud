"""兼容旧版 RAG 对话路由.

新前端使用 ``/assistant``; 这里保留旧的扁平 SSE 字段, 同时通过
Assistant FastEngine 适配器执行同一条 RAG 管线.
"""

import json
from collections.abc import AsyncGenerator
from contextlib import aclosing
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from loguru import logger

from app.api.dependencies import get_current_user, get_current_workspace
from app.exceptions import AuthenticationError, PermissionDeniedError
from app.features.assistant.engines.base import EngineRequest
from app.features.assistant.engines.fast_engine import FastEngine
from app.features.chat.schemas import ChatRequest
from app.features.chat.service import generate_chat_events
from app.features.chat.stream_authorization import authorized_events
from app.infra.extensions import SessionFactory, get_session_factory
from app.models.user import User
from app.models.workspace import Workspace

router = APIRouter(tags=["chat"])


@router.post("/chat", response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}}})
async def chat(
    payload: ChatRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    workspace: Annotated[Workspace, Depends(get_current_workspace)],
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
) -> StreamingResponse:
    """按工作空间文件检索并流式返回答案 (text/event-stream)."""
    if not payload.query:
        message = "Query is required"
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=message,
        )

    async def stream() -> AsyncGenerator[str, None]:
        engine = FastEngine(legacy_generator=generate_chat_events)
        iterator = engine.stream(
            EngineRequest(
                run_id=0,
                user_id=current_user.id,
                workspace_id=workspace.id,
                query=payload.query,
                history=payload.history,
            ),
        )
        try:
            async with aclosing(authorized_events(
                iterator, session_factory, int(workspace.id),
                int(current_user.id), current_user.http_stream_credential,
            )) as events:
                async for event in events:
                    legacy_payload: dict[str, object] = {
                        "type": event.type, **event.payload,
                    }
                    if event.type == "error":
                        detail = event.payload.get("message", "请求失败")
                        legacy_payload = {
                            "type": "status",
                            "content": f"出错了: {detail}",
                        }
                    yield (
                        f"data: {json.dumps(legacy_payload, ensure_ascii=False)}"
                        "\n\n"
                    )
        except AuthenticationError:
            frame: dict[str, object] = {
                "type": "status",
                "content": "登录状态已失效, 对话已停止, 请重新登录。",
            }
            yield f"data: {json.dumps(frame, ensure_ascii=False)}\n\n"
        except PermissionDeniedError:
            frame = {
                "type": "status",
                "content": "工作空间访问权限已撤销, 对话已停止。",
            }
            yield f"data: {json.dumps(frame, ensure_ascii=False)}\n\n"
        except Exception as exc:
            logger.error(
                "Legacy chat stream failed (error_type={})", type(exc).__name__,
            )
            frame = {"type": "status", "content": "对话请求失败, 请稍后重试。"}
            yield f"data: {json.dumps(frame, ensure_ascii=False)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")
