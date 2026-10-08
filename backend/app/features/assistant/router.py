"""Unified Assistant HTTP API."""

import os
from contextlib import aclosing

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.api.dependencies import get_current_user, get_current_workspace
from app.features.assistant import service
from app.features.assistant.event_protocol import encode_sse
from app.features.assistant.schemas import (
    AssistantConversationResponse, AssistantConversationListResponse,
    AssistantMessagesResponse, AssistantRunResponse,
    AssistantConversationCreate,
    AssistantConversationUpdate,
    AssistantHandoffRequest,
    AssistantMessageRequest,
    AssistantPermissionResponse,
)
from app.infra.extensions import SessionLocal, get_db
from app.models.user import User
from app.models.workspace import Workspace

from app.api.contracts import SuccessResponse, success_payload

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


@router.post("/assistant/conversations", status_code=status.HTTP_201_CREATED, response_model=SuccessResponse[AssistantConversationResponse])
def create_assistant_conversation(
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
    return success_payload(conversation.to_dict())


@router.get("/assistant/conversations", response_model=SuccessResponse[AssistantConversationListResponse])
def list_assistant_conversations(
    mode: str | None = None,
    include_archived: bool = False,
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
    conversations = service.list_user_conversations(
        session,
        workspace,
        current_user.id,
        mode,
        include_archived=include_archived,
    )
    return success_payload({"conversations": [conversation.to_dict() for conversation in conversations]})


@router.patch("/assistant/conversations/{conversation_id}", response_model=SuccessResponse[AssistantConversationResponse])
def update_assistant_conversation(
    conversation_id: int,
    payload: AssistantConversationUpdate,
    current_user: User = Depends(get_current_user),
    workspace: Workspace = Depends(get_current_workspace),
    session: Session = Depends(get_db),
):
    _require_v2()
    conversation = service.update_conversation(
        session,
        workspace,
        current_user.id,
        conversation_id,
        title=payload.title,
        status=payload.status,
    )
    return success_payload(conversation.to_dict())


@router.delete("/assistant/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_assistant_conversation(
    conversation_id: int,
    current_user: User = Depends(get_current_user),
    workspace: Workspace = Depends(get_current_workspace),
    session: Session = Depends(get_db),
):
    _require_v2()
    service.delete_conversation(session, workspace, current_user.id, conversation_id)
    return None


@router.get("/assistant/conversations/{conversation_id}/messages", response_model=SuccessResponse[AssistantMessagesResponse])
def list_assistant_messages(
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
    return success_payload({
        "conversation": conversation.to_dict(),
        "messages": [message.to_dict() for message in messages],
    })


@router.get("/assistant/conversations/{conversation_id}/active-run", response_model=SuccessResponse[AssistantRunResponse | None])
def get_active_assistant_run_for_conversation(
    conversation_id: int,
    current_user: User = Depends(get_current_user),
    workspace: Workspace = Depends(get_current_workspace),
    session: Session = Depends(get_db),
):
    _require_v2()
    run = service.get_active_run_for_conversation(
        session,
        workspace,
        current_user.id,
        conversation_id,
    )
    return success_payload(run.to_dict() if run else None)


@router.post("/assistant/conversations/{conversation_id}/messages/stream", response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}}})
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
    def prepare_stream():
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
        return int(workspace.id), int(current_user.id), int(conversation.id), int(run.id), history

    # Resolve all commit-expired attributes in the blocking preparation unit.
    stream_workspace_id, stream_user_id, stream_conversation_id, stream_run_id, history = (
        await run_in_threadpool(prepare_stream)
    )

    async def stream():
        async with aclosing(service.stream_run(
            SessionLocal, stream_workspace_id, stream_user_id,
            stream_conversation_id, stream_run_id, history,
        )) as events:
            async for event in events:
                yield encode_sse(event)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Assistant-Run-Id": str(stream_run_id),
        },
    )


@router.post("/assistant/runs/{run_id}/cancel", response_model=SuccessResponse[AssistantRunResponse])
async def cancel_assistant_run(
    run_id: int,
    current_user: User = Depends(get_current_user),
    workspace: Workspace = Depends(get_current_workspace),
    session: Session = Depends(get_db),
):
    _require_v2()
    def persist_cancel():
        # Keep commit-expired ORM serialization in the same blocking unit.
        return service.request_cancel(session, workspace, current_user.id, run_id).to_dict()

    result = await run_in_threadpool(persist_cancel)
    await service.run_registry.request_cancel(run_id)
    return success_payload(result)


@router.get("/assistant/runs/active", response_model=SuccessResponse[AssistantRunResponse | None])
def get_active_assistant_run_for_member(
    current_user: User = Depends(get_current_user),
    workspace: Workspace = Depends(get_current_workspace),
    session: Session = Depends(get_db),
):
    _require_v2()
    run = service.get_active_expert_run_for_member(session, workspace, current_user.id)
    return success_payload(run.to_dict() if run else None)


@router.get("/assistant/runs/{run_id}", response_model=SuccessResponse[AssistantRunResponse])
def get_assistant_run(
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
    return success_payload(run.to_dict())


@router.post("/assistant/runs/{run_id}/permissions/{permission_id}", response_model=SuccessResponse[AssistantRunResponse])
def respond_assistant_permission(
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
    return success_payload(run.to_dict())


@router.post("/assistant/conversations/{conversation_id}/handoff", status_code=status.HTTP_201_CREATED, response_model=SuccessResponse[AssistantConversationResponse])
def handoff_assistant_conversation(
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
    return success_payload(conversation.to_dict())
