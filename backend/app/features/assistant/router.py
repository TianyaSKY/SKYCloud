"""统一助手 HTTP API."""

import asyncio
import os
from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from runtime_common.http_stream import drain_cleanup

from app.api.contracts import SuccessEnvelope, SuccessResponse, success_payload
from app.api.dependencies import (
    Ctx,
    get_current_user,
    get_current_workspace,
)
from app.features.assistant import repository, service
from app.features.assistant.event_protocol import encode_sse
from app.features.assistant.schemas import (
    AssistantConversationCreate,
    AssistantConversationListPayload,
    AssistantConversationListResponse,
    AssistantConversationResponse,
    AssistantConversationUpdate,
    AssistantHandoffRequest,
    AssistantMessageRequest,
    AssistantMessagesPayload,
    AssistantMessagesResponse,
    AssistantPermissionResponse,
    AssistantRunResponse,
)
from app.features.assistant.stream_response import (
    PreparedStreamResponse,
    preparation_session,
    prepare_response_run,
)
from app.features.workspace.permissions import assert_can_write, assert_member
from app.infra.extensions import SessionFactory, get_session_factory
from app.infra.owned_work import run_owned_persistent
from app.models.assistant import AssistantConversationDict, AssistantRunDict
from app.models.user import User
from app.models.workspace import Workspace

router = APIRouter(tags=["assistant"])

CurrentUser = Annotated[User, Depends(get_current_user)]
CurrentWorkspace = Annotated[Workspace, Depends(get_current_workspace)]
SessionFactoryDep = Annotated[SessionFactory, Depends(get_session_factory)]


def _enabled(name: str, *, default: bool = True) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _require_v2() -> None:
    """当助手 API 被禁用时拒绝所有路由."""
    if not _enabled("ASSISTANT_V2_ENABLED"):
        message = "Assistant is disabled"
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=message,
        )


def _require_expert() -> None:
    """当专家模式被禁用时拒绝专家路由."""
    if not _enabled("ASSISTANT_EXPERT_ENABLED"):
        message = "Expert mode is disabled"
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=message,
        )


def _require_permission_ui() -> None:
    """当 UI 禁用时拒绝权限响应."""
    if not _enabled("ASSISTANT_PERMISSION_UI_ENABLED"):
        message = "Assistant permission confirmation is disabled"
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=message,
        )


@router.post(
    "/assistant/conversations",
    status_code=status.HTTP_201_CREATED,
    response_model=SuccessResponse[AssistantConversationResponse],
)
def create_assistant_conversation(
    payload: AssistantConversationCreate,
    current_user: CurrentUser,
    workspace: CurrentWorkspace,
    session_factory: SessionFactoryDep,
) -> SuccessEnvelope[AssistantConversationDict]:
    """创建所属的单个助手对话."""
    with session_factory() as session:
        _require_v2()
        if payload.mode == "expert":
            _require_expert()
        conversation = service.create_conversation(
            session,
            workspace,
            current_user.id,
            payload.mode,
            payload.title,
        )
        return success_payload(conversation.to_dict())


@router.get(
    "/assistant/conversations",
    response_model=SuccessResponse[AssistantConversationListResponse],
)
def list_assistant_conversations(
    mode: str | None = None,
    *,
    include_archived: bool = False,
    current_user: CurrentUser,
    workspace: CurrentWorkspace,
    session_factory: SessionFactoryDep,
) -> SuccessEnvelope[AssistantConversationListPayload]:
    """列出调用方在当前工作区中的对话."""
    with session_factory() as session:
        _require_v2()
        if mode not in {None, "fast", "expert"}:
            message = "Invalid assistant mode"
            raise HTTPException(status_code=400, detail=message)
        if mode == "expert":
            _require_expert()
        elif mode is None and not _enabled("ASSISTANT_EXPERT_ENABLED"):
            mode = "fast"
        conversations = service.list_user_conversations(
            session,
            workspace,
            current_user.id,
            mode,
            include_archived=include_archived,
        )
        return success_payload(
            {
                "conversations": [
                    conversation.to_dict() for conversation in conversations
                ],
            },
        )


@router.patch(
    "/assistant/conversations/{conversation_id}",
    response_model=SuccessResponse[AssistantConversationResponse],
)
def update_assistant_conversation(
    conversation_id: int,
    payload: AssistantConversationUpdate,
    current_user: CurrentUser,
    workspace: CurrentWorkspace,
    session_factory: SessionFactoryDep,
) -> SuccessEnvelope[AssistantConversationDict]:
    """重命名或归档所属的单个对话."""
    with session_factory() as session:
        _require_v2()
        conversation = service.update_conversation(
            session,
            service.UpdateConversationParams(
                workspace=workspace,
                user_id=current_user.id,
                conversation_id=conversation_id,
                title=payload.title,
                status=payload.status,
            ),
        )
        return success_payload(conversation.to_dict())


@router.delete(
    "/assistant/conversations/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_assistant_conversation(
    conversation_id: int,
    current_user: CurrentUser,
    workspace: CurrentWorkspace,
    session_factory: SessionFactoryDep,
) -> None:
    """删除无活跃运行的所属对话."""
    with session_factory() as session:
        _require_v2()
        service.delete_conversation(
            session,
            workspace,
            current_user.id,
            conversation_id,
        )


@router.get(
    "/assistant/conversations/{conversation_id}/messages",
    response_model=SuccessResponse[AssistantMessagesResponse],
)
def list_assistant_messages(
    conversation_id: int,
    current_user: CurrentUser,
    workspace: CurrentWorkspace,
    session_factory: SessionFactoryDep,
) -> SuccessEnvelope[AssistantMessagesPayload]:
    """返回所属对话的消息列表."""
    with session_factory() as session:
        _require_v2()
        conversation = service.get_user_conversation(
            session,
            workspace,
            current_user.id,
            conversation_id,
        )
        if conversation.mode == "expert":
            _require_expert()
        messages = service.get_conversation_messages(
            session,
            workspace,
            current_user.id,
            conversation_id,
        )
        return success_payload(
            {
                "conversation": conversation.to_dict(),
                "messages": [message.to_dict() for message in messages],
            },
        )


@router.get(
    "/assistant/conversations/{conversation_id}/active-run",
    response_model=SuccessResponse[AssistantRunResponse | None],
)
def get_active_assistant_run_for_conversation(
    conversation_id: int,
    current_user: CurrentUser,
    workspace: CurrentWorkspace,
    session_factory: SessionFactoryDep,
) -> SuccessEnvelope[AssistantRunDict | None]:
    """返回单个对话的活跃运行 (若存在)."""
    with session_factory() as session:
        _require_v2()
        run = service.get_active_run_for_conversation(
            session,
            workspace,
            current_user.id,
            conversation_id,
        )
        return success_payload(run.to_dict() if run else None)


@router.post(
    "/assistant/conversations/{conversation_id}/messages/stream",
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {
                "text/event-stream": {"schema": {"type": "string"}},
            },
        },
    },
)
async def stream_assistant_message(
    conversation_id: int,
    payload: AssistantMessageRequest,
    request: Request,
    ctx: Ctx,
    workspace: CurrentWorkspace,
) -> PreparedStreamResponse:
    """准备单次运行并流式传输其稳定的 SSE 事件."""
    _require_v2()
    if not payload.query.strip():
        message = "Query is required"
        raise HTTPException(status_code=400, detail=message)
    current_user = ctx.user

    def prepare_stream(
        on_run_created: Callable[[tuple[int, int, int, int]], None],
    ) -> tuple[
        int,
        int,
        int,
        int,
        list[dict[str, str]],
    ]:
        with preparation_session(ctx.session_factory) as session:
            existing_conversation = service.get_user_conversation(
                session,
                workspace,
                current_user.id,
                conversation_id,
            )
            if existing_conversation.mode == "expert":
                _require_expert()
            conversation, run, history = service.prepare_run(
                session,
                service.PrepareRunParams(
                    workspace=workspace,
                    user_id=current_user.id,
                    conversation_id=conversation_id,
                    query=payload.query,
                    request_id=request.headers.get("X-Request-Id"),
                    on_run_created=on_run_created,
                ),
            )
            return (
                int(workspace.id),
                int(current_user.id),
                int(conversation.id),
                int(run.id),
                history,
            )

    # 在阻塞准备单元中解析所有 commit 后过期的属性.
    (
        stream_workspace_id,
        stream_user_id,
        stream_conversation_id,
        stream_run_id,
        history,
    ) = await prepare_response_run(prepare_stream, ctx.session_factory)
    credential = current_user.http_stream_credential

    async def stream() -> AsyncGenerator[str, None]:
        async with aclosing(
            service.stream_run(
                service.StreamRunParams(
                    session_factory=ctx.session_factory,
                    workspace_id=stream_workspace_id,
                    user_id=stream_user_id,
                    conversation_id=stream_conversation_id,
                    run_id=stream_run_id,
                    history=history,
                    credential=credential,
                ),
            ),
        ) as events:
            async for event in events:
                yield encode_sse(event)

    return PreparedStreamResponse(
        stream(),
        session_factory=ctx.session_factory,
        run_scope=(
            stream_workspace_id,
            stream_user_id,
            stream_conversation_id,
            stream_run_id,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Assistant-Run-Id": str(stream_run_id),
        },
    )


@router.post(
    "/assistant/runs/{run_id}/cancel",
    response_model=SuccessResponse[AssistantRunResponse],
)
async def cancel_assistant_run(
    run_id: int,
    current_user: CurrentUser,
    workspace: CurrentWorkspace,
    session_factory: SessionFactoryDep,
) -> SuccessEnvelope[AssistantRunDict]:
    """持久化取消状态并向存活生产者发出信号."""
    _require_v2()

    def persist_cancel() -> AssistantRunDict:
        with session_factory() as session:
            # 将 commit 后过期的 ORM 序列化保留在同一阻塞单元中.
            return service.request_cancel(
                session,
                workspace,
                current_user.id,
                run_id,
            ).to_dict()

    async def cancel() -> AssistantRunDict:
        result = await run_owned_persistent(persist_cancel)
        await service.run_registry.request_cancel(run_id)
        return result

    # 一旦被接受, 持久化与本地生产者信号共享同一生命周期.
    # 丢失 HTTP 响应绝不能跳过生产者信号.
    pending = asyncio.create_task(cancel())
    try:
        result = await asyncio.shield(pending)
    except asyncio.CancelledError:
        async def drain() -> None:
            await asyncio.gather(pending, return_exceptions=True)

        await drain_cleanup(drain())
        raise
    return success_payload(result)


@router.get(
    "/assistant/runs/active",
    response_model=SuccessResponse[AssistantRunResponse | None],
)
def get_active_assistant_run_for_member(
    current_user: CurrentUser,
    workspace: CurrentWorkspace,
    session_factory: SessionFactoryDep,
) -> SuccessEnvelope[AssistantRunDict | None]:
    """返回调用方的活跃专家运行 (若存在)."""
    with session_factory() as session:
        _require_v2()
        run = service.get_active_expert_run_for_member(
            session,
            workspace,
            current_user.id,
        )
        return success_payload(run.to_dict() if run else None)


@router.get(
    "/assistant/runs/{run_id}",
    response_model=SuccessResponse[AssistantRunResponse],
)
def get_assistant_run(
    run_id: int,
    current_user: CurrentUser,
    workspace: CurrentWorkspace,
    session_factory: SessionFactoryDep,
) -> SuccessEnvelope[AssistantRunDict]:
    """返回所属的单次运行."""
    with session_factory() as session:
        _require_v2()
        assert_member(session, int(workspace.id), int(current_user.id))
        run = repository.get_owned_run(
            session,
            run_id,
            int(workspace.id),
            int(current_user.id),
        )
        if run is None:
            message = "Assistant run not found"
            raise HTTPException(status_code=404, detail=message)
        if run.engine == "expert":
            _require_expert()
            assert_can_write(session, int(workspace.id), int(current_user.id))
        return success_payload(run.to_dict())


@router.post(
    "/assistant/runs/{run_id}/permissions/{permission_id}",
    response_model=SuccessResponse[AssistantRunResponse],
)
def respond_assistant_permission(
    run_id: int,
    permission_id: str,
    payload: AssistantPermissionResponse,
    ctx: Ctx,
    workspace: CurrentWorkspace,
) -> SuccessEnvelope[AssistantRunDict]:
    """提交单次一次性权限响应."""
    with ctx.session_factory() as session:
        _require_v2()
        _require_permission_ui()
        run = service.respond_permission(
            session,
            service.RespondPermissionParams(
                workspace=workspace,
                user_id=ctx.user.id,
                run_id=run_id,
                permission_id=permission_id,
                response=payload.response,
                remember=payload.remember,
            ),
        )
        return success_payload(run.to_dict())


@router.post(
    "/assistant/conversations/{conversation_id}/handoff",
    status_code=status.HTTP_201_CREATED,
    response_model=SuccessResponse[AssistantConversationResponse],
)
def handoff_assistant_conversation(
    conversation_id: int,
    payload: AssistantHandoffRequest,
    current_user: CurrentUser,
    workspace: CurrentWorkspace,
    session_factory: SessionFactoryDep,
) -> SuccessEnvelope[AssistantConversationDict]:
    """创建接续快速对话的专家对话."""
    with session_factory() as session:
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
