"""Assistant orchestration: ownership, persistence, cancellation, and SSE."""

import asyncio
import os
import math
import time
import uuid
from contextlib import aclosing
from collections.abc import Callable, Sequence
from typing import Any

from sqlalchemy.orm import Session
from loguru import logger

from app.exceptions import (
    AuthenticationError,
    BusinessRuleError,
    ConflictError,
    PermissionDeniedError,
    ResourceNotFoundError,
)
from app.features.assistant import repository
from app.features.assistant import abort_repository
from app.features.assistant.engines.fast_engine import FastEngine
from app.features.assistant.event_protocol import AssistantEvent
from app.features.assistant.run_control import RunController, RunRegistry, run_registry
from app.features.assistant.stream_events import engine_events_with_heartbeats as _engine_events_with_heartbeats
from app.features.workspace.permissions import assert_can_write, assert_member, assert_can_write_locked, assert_member_locked
from app.infra.datetime_utils import utc_now, to_utc_datetime
from app.models.assistant import AssistantConversation, AssistantMessage, AssistantRun
from app.models.workspace import Workspace


def _recover_abandoned_run(session: Session, run: AssistantRun, *, commit: bool = True) -> bool:
    """Release a run left active after its SSE worker has disappeared.

    A browser disconnect normally cancels the generator and records a terminal
    status.  A process restart or an exception before the generator starts can
    leave a durable ``running`` row behind, which would otherwise permanently
    block its conversation.  Keep a short grace period so a duplicate submit
    still receives the normal conflict response.
    """

    if run_registry.get(int(run.id)) is not None:
        return False
    # Read and lock the latest durable heartbeat. A process-local registry
    # cannot establish that a worker on another host has disappeared.
    run = repository.lock_run(session, int(run.id))
    if run is None or run.status in repository.TERMINAL_RUN_STATUSES:
        return False
    started_at = to_utc_datetime(run.started_at or run.created_at)
    if started_at is None:
        return False
    try:
        grace_seconds = max(int(os.getenv("ASSISTANT_ABANDONED_RUN_GRACE_SECONDS", "45")), 30)
    except ValueError:
        grace_seconds = 45
    from sqlalchemy import func, select
    now = session.scalar(select(func.clock_timestamp())) if session.get_bind().dialect.name == 'postgresql' else utc_now()
    heartbeat = run.metadata_dict.get("worker_heartbeat_unix")
    if isinstance(heartbeat, (float, int)) and not isinstance(heartbeat, bool) and math.isfinite(heartbeat):
        age = now.timestamp() - heartbeat
        if -5 <= age < grace_seconds:
            return False
    if (now - started_at).total_seconds() < grace_seconds:
        return False

    abort_repository.stage(session, run)
    assistant_message = session.get(AssistantMessage, run.assistant_message_id)
    if assistant_message is not None and assistant_message.status in {"pending", "streaming"}:
        assistant_message.status = "failed"
    run.pending_permission_id = None
    run.permission_response = None
    repository.update_run_status(
        run,
        "failed",
        error_code="stream_abandoned",
        error_message="助手请求在服务重启或连接中断后未完成，请重新发送。",
    )
    if commit:
        session.commit()
    else:
        session.flush()
    return True


def _conversation_or_404(
    session: Session, conversation_id: int, workspace_id: int, user_id: int
) -> AssistantConversation:
    conversation = repository.get_conversation(session, conversation_id, workspace_id, user_id)
    if conversation is None:
        raise ResourceNotFoundError("Assistant conversation not found")
    return conversation


def create_conversation(
    session: Session,
    workspace: Workspace,
    user_id: int,
    mode: str,
    title: str | None = None,
) -> AssistantConversation:
    assert_member_locked(session, int(workspace.id), int(user_id))
    if mode == "expert":
        assert_can_write(session, int(workspace.id), int(user_id))
    conversation = repository.create_conversation(
        session,
        int(workspace.id),
        int(user_id),
        mode,
        title,
    )
    session.commit()
    return conversation


def list_user_conversations(
    session: Session,
    workspace: Workspace,
    user_id: int,
    mode: str | None = None,
    include_archived: bool = False,
) -> list[AssistantConversation]:
    role = assert_member(session, int(workspace.id), int(user_id))
    if mode == "expert":
        assert_can_write(session, int(workspace.id), int(user_id))
    elif mode is None and role.value == "viewer":
        # Do not leak expert session/runtime metadata through the unfiltered
        # list endpoint.  Editors/admins may intentionally list both modes.
        mode = "fast"
    return repository.list_conversations(
        session,
        int(workspace.id),
        int(user_id),
        mode,
        include_archived=include_archived,
    )


def get_active_run_for_conversation(
    session: Session,
    workspace: Workspace,
    user_id: int,
    conversation_id: int,
) -> AssistantRun | None:
    assert_member_locked(session, int(workspace.id), int(user_id))
    conversation = get_user_conversation(session, workspace, user_id, conversation_id)
    run = repository.active_run_for_conversation(session, int(conversation.id))
    if run and _recover_abandoned_run(session, run):
        return None
    return run


def get_active_expert_run_for_member(
    session: Session,
    workspace: Workspace,
    user_id: int,
) -> AssistantRun | None:
    assert_can_write_locked(session, int(workspace.id), int(user_id))
    run = repository.active_expert_run_for_member(session, int(workspace.id), int(user_id))
    if run and _recover_abandoned_run(session, run):
        return None
    return run


def get_user_conversation(
    session: Session,
    workspace: Workspace,
    user_id: int,
    conversation_id: int,
) -> AssistantConversation:
    assert_member(session, int(workspace.id), int(user_id))
    conversation = _conversation_or_404(session, conversation_id, int(workspace.id), int(user_id))
    if conversation.mode == "expert":
        assert_can_write(session, int(workspace.id), int(user_id))
    return conversation


def get_conversation_messages(
    session: Session,
    workspace: Workspace,
    user_id: int,
    conversation_id: int,
) -> list[AssistantMessage]:
    conversation = get_user_conversation(session, workspace, user_id, conversation_id)
    return repository.get_messages(session, int(conversation.id))


def update_conversation(
    session: Session,
    workspace: Workspace,
    user_id: int,
    conversation_id: int,
    *,
    title: str | None = None,
    status: str | None = None,
) -> AssistantConversation:
    assert_member_locked(session, int(workspace.id), int(user_id))
    get_user_conversation(session, workspace, user_id, conversation_id)
    conversation = repository.lock_conversation(
        session,
        conversation_id,
        int(workspace.id),
        int(user_id),
    )
    if conversation is None:
        raise ResourceNotFoundError("Assistant conversation not found")
    if title is not None:
        normalized_title = title.strip()
        if not normalized_title:
            raise BusinessRuleError("会话标题不能为空")
        conversation.title = normalized_title

    if status is not None:
        if status not in {"active", "archived"}:
            raise BusinessRuleError("会话状态无效")
        active_run = repository.active_run_for_conversation(session, int(conversation.id))
        if active_run and _recover_abandoned_run(session, active_run, commit=False):
            active_run = None
        if active_run and status == "archived":
            raise ConflictError("当前会话仍有任务运行，请先停止任务")
        conversation.status = status

    session.commit()
    return conversation


def delete_conversation(
    session: Session,
    workspace: Workspace,
    user_id: int,
    conversation_id: int,
) -> None:
    assert_member_locked(session, int(workspace.id), int(user_id))
    get_user_conversation(session, workspace, user_id, conversation_id)
    conversation = repository.lock_conversation(
        session,
        conversation_id,
        int(workspace.id),
        int(user_id),
    )
    if conversation is None:
        raise ResourceNotFoundError("Assistant conversation not found")
    active_run = repository.active_run_for_conversation(session, int(conversation.id))
    if active_run and _recover_abandoned_run(session, active_run, commit=False):
        active_run = None
    if active_run:
        raise ConflictError("当前会话仍有任务运行，请先停止任务")
    session.delete(conversation)
    session.commit()


def prepare_run(
    session: Session,
    workspace: Workspace,
    user_id: int,
    conversation_id: int,
    query: str,
    request_id: str | None = None,
    *,
    on_run_created: Callable[[tuple[int, int, int, int]], None] | None = None,
) -> tuple[AssistantConversation, AssistantRun, list[dict[str, str]]]:
    """Create the durable message/run records before opening the SSE stream."""

    assert_member_locked(session, int(workspace.id), int(user_id))
    conversation = get_user_conversation(session, workspace, user_id, conversation_id)
    locked_conversation = repository.lock_conversation(
        session,
        conversation_id,
        int(workspace.id),
        int(user_id),
    )
    if locked_conversation is None:
        raise ResourceNotFoundError("Assistant conversation not found")
    conversation = locked_conversation
    if conversation.status != "active":
        raise ConflictError("会话已归档，请先恢复后再发送")
    if conversation.mode == "expert":
        assert_can_write(session, int(workspace.id), int(user_id))
        active_member_run = repository.active_expert_run_for_member(
            session, int(workspace.id), int(user_id)
        )
        if active_member_run and _recover_abandoned_run(session, active_member_run, commit=False):
            active_member_run = None
        if active_member_run:
            raise ConflictError("Expert runtime is already busy")

    active = repository.active_run_for_conversation(session, int(conversation.id))
    if active and _recover_abandoned_run(session, active, commit=False):
        active = None
    if active:
        raise ConflictError("This assistant conversation already has a running task")

    if conversation.mode == 'expert' and abort_repository.pending_for_member(session, int(workspace.id), int(user_id)):
        raise ConflictError('Expert runtime cleanup is still pending')

    # The history excludes the new user message and pending assistant shell.
    history = [
        {"role": message.role, "content": message.content}
        for message in repository.get_messages(session, int(conversation.id), limit=20)
        if message.status in {"completed", "failed", "cancelled"}
    ]
    user_message = repository.create_message(
        session, conversation, "user", query.strip(), status="completed"
    )
    assistant_message = repository.create_message(
        session, conversation, "assistant", "", status="pending"
    )
    run = repository.create_run(
        session,
        conversation,
        user_message,
        assistant_message,
        request_id=request_id or str(uuid.uuid4()),
    )
    repository.update_run_status(run, "running")
    assistant_message.status = "streaming"
    if on_run_created is not None:
        # create_run has flushed its ID. Capture internal identity before commit
        # so a lost acknowledgement can be compensated in a fresh transaction.
        on_run_created((int(workspace.id), int(user_id), int(conversation.id), int(run.id)))
    session.commit()
    return conversation, run, history


def _event_content(event: AssistantEvent) -> str:
    value = event.payload.get("content")
    return value if isinstance(value, str) else ""


async def _stream_persisted(worker, persistence, history):
    try:
        run_id, message_id, workspace_id, mode, query = await worker.call(persistence.initialize, time.time())
    except asyncio.CancelledError:
        await worker.call(persistence.finish, "cancelled", "", {}, {}, [], None)
        raise
    except AuthenticationError:
        status = await worker.call(persistence.finish, "cancelled", "", {}, {}, [], None)
        if status == "cancelled":
            yield AssistantEvent("cancelled", {"status": status},
                                 run_id=persistence.run_id, message_id=persistence.message_id)
        return
    controller = run_registry.register(run_id)
    event_log = []
    answer_parts = []
    metadata = {}
    usage = {}
    last_commit = last_lease = time.monotonic()
    event_count = 0

    async def finish(status, error_code=None):
        return await worker.call(persistence.finish, status, "".join(answer_parts),
                                 dict(metadata), dict(usage), list(event_log), error_code)

    async def poll_idle():
        nonlocal last_lease
        renew = time.monotonic() - last_lease >= 10
        terminal, cancelled = await worker.call(persistence.poll, renew, time.time(), True)
        if renew:
            last_lease = time.monotonic()
        if terminal or cancelled or controller.cancel_event.is_set():
            raise PermissionDeniedError("Assistant stream authorization ended")

    try:
        yield AssistantEvent("run_started", {"engine": mode, "status": "running"},
                             run_id=run_id, message_id=message_id)
        engine = await worker.call(persistence.engine)
        iterator = engine.stream(run_id=run_id, user_id=persistence.user_id,
                                 workspace_id=workspace_id, query=query, history=history, controller=controller)
        async with aclosing(_engine_events_with_heartbeats(
            iterator, run_id, on_idle=poll_idle, on_abort=controller.request_cancel,
        )) as engine_events:
            async for event in engine_events:
                renew = time.monotonic() - last_lease >= 10
                terminal, cancelled = await worker.call(persistence.poll, renew, time.time())
                if terminal:
                    await controller.request_cancel()
                    return
                if renew:
                    last_lease = time.monotonic()
                if event.run_id is None:
                    event.run_id = run_id
                if event.message_id is None and event.type != "heartbeat":
                    event.message_id = message_id
                if event.type != "heartbeat":
                    event_log.append(event)
                if event.type == "cancelled" or controller.cancel_event.is_set() or cancelled:
                    await controller.request_cancel()
                    status = await finish("cancelled")
                    if status == "cancelled":
                        yield AssistantEvent("cancelled", {"status": status}, run_id=run_id, message_id=message_id)
                    return
                if event.type == "token":
                    answer_parts.append(_event_content(event))
                elif event.type in {"title", "keywords"}:
                    metadata[event.type] = _event_content(event)
                elif event.type == "sources":
                    metadata["sources"] = event.payload.get("sources", [])
                elif event.type == "usage":
                    usage.update(event.payload)
                elif event.type == "error":
                    raise RuntimeError(str(event.payload.get("message") or "assistant engine error"))
                elif event.type == "file_diff":
                    metadata["diff"] = event.payload.get("files", event.payload)
                event_count += 1
                commit = event_count % 20 == 0 or time.monotonic() - last_commit > 1
                permission = event.payload if event.type == "permission_required" else None
                if not await worker.call(persistence.checkpoint, "".join(answer_parts),
                                         dict(metadata), dict(usage), permission, commit):
                    return
                if commit or permission is not None:
                    last_commit = time.monotonic()
                if event.type != "done":
                    yield event
        status = await finish("completed")
        if status in {"completed", "cancelled"}:
            yield AssistantEvent("cancelled" if status == "cancelled" else "done",
                                 {"status": status}, run_id=run_id, message_id=message_id)
    except (asyncio.CancelledError, GeneratorExit):
        await finish("cancelled")
        raise
    except (AuthenticationError, PermissionDeniedError):
        if not controller.cancel_event.is_set():
            await controller.request_cancel()
        status = await finish("cancelled")
        if status == "cancelled":
            yield AssistantEvent("cancelled", {"status": status}, run_id=run_id, message_id=message_id)
    except Exception as exc:
        status = await finish("failed", exc.__class__.__name__)
        if status == "failed":
            yield AssistantEvent("error", {"code": exc.__class__.__name__,
                "message": "专家任务执行失败" if mode == "expert" else "快速问答执行失败"},
                run_id=run_id, message_id=message_id)
    finally:
        run_registry.unregister(run_id)


def _cleanup_stream_session(session: Session, operation: str) -> None:
    """Run cleanup on the owning worker without replacing the stream outcome."""
    try:
        getattr(session, operation)()
    except Exception as exc:
        logger.error("Assistant stream session {} failed (error_type={})", operation, type(exc).__name__)
        try:
            session.invalidate()
        except Exception as invalidation_exc:
            logger.error("Assistant stream session invalidation failed (error_type={})", type(invalidation_exc).__name__)


async def stream_prepared_run(session, workspace, user_id, conversation, run, history):
    """Compatibility seam for callers that own an existing prepared Session.

    Callers must leave that Session idle until this iterator closes. HTTP
    streams use stream_run, which creates and closes its Session in the worker.
    """
    from app.features.assistant.stream_persistence import StreamPersistence, StreamWorker
    async with StreamWorker() as worker:
        try:
            persistence = await worker.call(StreamPersistence, session, workspace, user_id, conversation, run)
            async with aclosing(_stream_persisted(worker, persistence, history)) as events:
                async for event in events:
                    yield event
        finally:
            await worker.call(_cleanup_stream_session, session, "rollback")


async def stream_run(session_factory, workspace_id, user_id, conversation_id, run_id, history, *, credential=None):
    """Own the streaming Session entirely within its dedicated worker thread."""
    from app.features.assistant.stream_persistence import StreamPersistence, StreamWorker
    async with StreamWorker() as worker:
        owned_sessions = []
        def create():
            session = session_factory()
            owned_sessions.append(session)
            workspace = session.get(Workspace, workspace_id)
            conversation = session.get(AssistantConversation, conversation_id)
            run = session.get(AssistantRun, run_id)
            if workspace is None or conversation is None or run is None:
                raise ResourceNotFoundError("Assistant stream records disappeared")
            persistence = StreamPersistence(session, workspace, user_id, conversation, run)
            persistence.credential = credential
            return persistence
        try:
            persistence = await worker.call(create)
            async with aclosing(_stream_persisted(worker, persistence, history)) as events:
                async for event in events:
                    yield event
        finally:
            for session in owned_sessions:
                await worker.call(_cleanup_stream_session, session, "close")


def request_cancel(
    session: Session,
    workspace: Workspace,
    user_id: int,
    run_id: int,
) -> AssistantRun:
    assert_member_locked(session, int(workspace.id), int(user_id))
    run = repository.get_owned_run(session, run_id, int(workspace.id), int(user_id))
    if run is None:
        raise ResourceNotFoundError("Assistant run not found")
    run = repository.lock_run(session, run_id)
    if run is None:
        raise ResourceNotFoundError("Assistant run not found")
    if run.engine == "expert":
        assert_can_write(session, int(workspace.id), int(user_id))
    if run.status in repository.TERMINAL_RUN_STATUSES:
        return run
    abort_repository.stage(session, run)
    run.cancel_requested = True
    session.commit()
    return run


def respond_permission(
    session: Session,
    workspace: Workspace,
    user_id: int,
    run_id: int,
    permission_id: str,
    response: str,
    remember: bool = False,
) -> AssistantRun:
    assert_member_locked(session, int(workspace.id), int(user_id))
    run = repository.get_owned_run(session, run_id, int(workspace.id), int(user_id))
    if run is None:
        raise ResourceNotFoundError("Assistant run not found")
    if run.engine == "expert":
        assert_can_write(session, int(workspace.id), int(user_id))
    if run.engine != "expert" or run.status != "waiting_permission":
        raise ConflictError("This run is not waiting for a permission response")
    if run.pending_permission_id != permission_id:
        raise PermissionDeniedError("Permission request does not belong to this run")
    # The provider receives only the one-shot response.  ``remember`` is
    # accepted for API compatibility but deliberately not persisted as a
    # permanent allow rule in the first version.
    del remember
    # Persist first so a different API worker can wake the engine.  The local
    # future below is only the low-latency path for same-worker requests.
    if not repository.set_permission_response(session, run_id, permission_id, response):
        raise ConflictError("Permission request has already been answered")
    session.commit()
    # A worker-thread notification can wake the stream immediately. Never
    # authorize provider execution before the durable transaction succeeds.
    run_registry.resolve_permission(run_id, permission_id, response)
    return run


def handoff_to_expert(
    session: Session,
    workspace: Workspace,
    user_id: int,
    source_conversation_id: int,
    message_id: int | None = None,
) -> AssistantConversation:
    assert_can_write_locked(session, int(workspace.id), int(user_id))
    source = get_user_conversation(session, workspace, user_id, source_conversation_id)
    if source.mode != "fast":
        raise ConflictError("Only a fast conversation can be handed off")
    assert_can_write(session, int(workspace.id), int(user_id))

    messages = repository.get_messages(session, int(source.id), limit=10)
    if message_id is not None and repository.get_message_for_conversation(
        session, message_id, int(source.id)
    ) is None:
        raise ResourceNotFoundError("Handoff message not found")

    lines = [
        "以下内容来自 SKYCloud 快速问答，请在此基础上继续处理。",
        "",
    ]
    for message in messages:
        if message.role in {"user", "assistant"} and message.content.strip():
            lines.append(f"{message.role}: {message.content.strip()}")
        sources = message.metadata_dict.get("sources", [])
        if isinstance(sources, list):
            for source_item in sources[:8]:
                if isinstance(source_item, dict):
                    name = source_item.get("file_name") or source_item.get("file_id")
                    page = source_item.get("page_number")
                    if name:
                        lines.append(f"相关文件: {name}" + (f"，第 {page} 页" if page else ""))
    context = "\n".join(lines)
    target = repository.create_conversation(
        session,
        int(workspace.id),
        int(user_id),
        "expert",
        title=f"专家继续：{source.title or '快速问答'}",
        source_conversation_id=int(source.id),
    )
    repository.create_message(
        session,
        target,
        "system",
        context,
        status="completed",
        metadata={"source_conversation_id": source.id, "handoff_message_id": message_id},
    )
    session.commit()
    return target
