"""Unified Assistant HTTP API."""

import os

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user, get_current_workspace
from app.features.assistant import service
from app.features.assistant.event_protocol import encode_sse
from app.features.assistant.schemas import (
    AssistantConversationCreate,
    AssistantHandoffRequest,
    AssistantMessageRequest,
    AssistantPermissionResponse,
)
from app.infra.extensions import get_db
from app.models.user import User
from app.models.workspace import Workspace

router = APIRouter(tags=["assistant"])


def _enabled(name: str, default: bool = True) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _require_v2() -> None:
    if not _enabled("ASSISTANT_V2_ENABLED", True):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Assistant is disabled")


def _require_expert() -> None:
    if not _enabled("ASSISTANT_EXPERT_ENABLED", True):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Expert mode is disabled")


def _require_permission_ui() -> None:
    if not _enabled("ASSISTANT_PERMISSION_UI_ENABLED", True):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Assistant permission confirmation is disabled",
        )


@router.post("/assistant/conversations", status_code=status.HTTP_201_CREATED)
async def create_assistant_conversation(
    payload: AssistantConversationCreate,
    current_user: User = Depends(get_current_user),
    workspace: Workspace = Depends(get_current_workspace),
    session: Session = Depends(get_db),
):
    _require_v2()
    if payload.mode == "expert":
        _require_expert()
    conversation = service.create_conversation(
        session, workspace, current_user.id, payload.mode, payload.title
    )
    return conversation.to_dict()


@router.get("/assistant/conversations")
async def list_assistant_conversations(
    mode: str | None = None,
    current_user: User = Depends(get_current_user),
    workspace: Workspace = Depends(get_current_workspace),
    session: Session = Depends(get_db),
):
    _require_v2()
    if mode not in {None, "fast", "expert"}:
        raise HTTPException(status_code=400, detail="Invalid assistant mode")
    if mode == "expert":
        _require_expert()
    elif mode is None and not _enabled("ASSISTANT_EXPERT_ENABLED", True):
        mode = "fast"
    conversations = service.list_user_conversations(session, workspace, current_user.id, mode)
    return {"conversations": [conversation.to_dict() for conversation in conversations]}


@router.get("/assistant/conversations/{conversation_id}/messages")
async def list_assistant_messages(
    conversation_id: int,
    current_user: User = Depends(get_current_user),
    workspace: Workspace = Depends(get_current_workspace),
    session: Session = Depends(get_db),
):
    _require_v2()
    conversation = service.get_user_conversation(session, workspace, current_user.id, conversation_id)
    if conversation.mode == "expert":
        _require_expert()
    messages = service.get_conversation_messages(session, workspace, current_user.id, conversation_id)
    return {
        "conversation": conversation.to_dict(),
        "messages": [message.to_dict() for message in messages],
    }


@router.post("/assistant/conversations/{conversation_id}/messages/stream")
async def stream_assistant_message(
    conversation_id: int,
    payload: AssistantMessageRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    workspace: Workspace = Depends(get_current_workspace),
    session: Session = Depends(get_db),
):
    _require_v2()
    if not payload.query.strip():
        raise HTTPException(status_code=400, detail="Query is required")
    existing_conversation = service.get_user_conversation(
        session, workspace, current_user.id, conversation_id
    )
    if existing_conversation.mode == "expert":
        _require_expert()
    conversation, run, history = service.prepare_run(
        session,
        workspace,
        current_user.id,
        conversation_id,
        payload.query,
        request.headers.get("X-Request-Id"),
    )
    async def stream():
        async for event in service.stream_prepared_run(
            session,
            workspace,
            current_user.id,
            conversation,
            run,
            history,
        ):
            yield encode_sse(event)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Assistant-Run-Id": str(run.id),
        },
    )


@router.post("/assistant/runs/{run_id}/cancel")
async def cancel_assistant_run(
    run_id: int,
    current_user: User = Depends(get_current_user),
    workspace: Workspace = Depends(get_current_workspace),
    session: Session = Depends(get_db),
):
    _require_v2()
    run = service.request_cancel(session, workspace, current_user.id, run_id)
    await service.run_registry.request_cancel(run_id)
    return run.to_dict()


@router.get("/assistant/runs/{run_id}")
async def get_assistant_run(
    run_id: int,
    current_user: User = Depends(get_current_user),
    workspace: Workspace = Depends(get_current_workspace),
    session: Session = Depends(get_db),
):
    _require_v2()
    from app.features.assistant import repository

    run = repository.get_owned_run(session, run_id, int(workspace.id), int(current_user.id))
    if run is None:
        raise HTTPException(status_code=404, detail="Assistant run not found")
    if run.engine == "expert":
        _require_expert()
        from app.features.workspace.permissions import assert_can_write

        assert_can_write(session, int(workspace.id), int(current_user.id))
    return run.to_dict()


@router.post("/assistant/runs/{run_id}/permissions/{permission_id}")
async def respond_assistant_permission(
    run_id: int,
    permission_id: str,
    payload: AssistantPermissionResponse,
    current_user: User = Depends(get_current_user),
    workspace: Workspace = Depends(get_current_workspace),
    session: Session = Depends(get_db),
):
    _require_v2()
    _require_permission_ui()
    run = service.respond_permission(
        session,
        workspace,
        current_user.id,
        run_id,
        permission_id,
        payload.response,
        payload.remember,
    )
    return run.to_dict()


@router.post("/assistant/conversations/{conversation_id}/handoff", status_code=status.HTTP_201_CREATED)
async def handoff_assistant_conversation(
    conversation_id: int,
    payload: AssistantHandoffRequest,
    current_user: User = Depends(get_current_user),
    workspace: Workspace = Depends(get_current_workspace),
    session: Session = Depends(get_db),
):
    _require_v2()
    _require_expert()
    conversation = service.handoff_to_expert(
        session,
        workspace,
        current_user.id,
        conversation_id,
        payload.message_id,
    )
    return conversation.to_dict()
