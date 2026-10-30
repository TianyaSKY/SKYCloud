"""Assistant orchestration: ownership, persistence, cancellation, and SSE."""

import asyncio
import math
import os
import time
import uuid
from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing
from dataclasses import dataclass
from datetime import datetime
from functools import partial

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.stream_credentials import StreamCredential
from app.exceptions import (
    AuthenticationError,
    BusinessRuleError,
    ConflictError,
    PermissionDeniedError,
    ResourceNotFoundError,
)
from app.features.assistant import abort_repository, repository
from app.features.assistant.engines.base import EngineRequest
from app.features.assistant.event_protocol import AssistantEvent
from app.features.assistant.run_control import RunController, run_registry
from app.features.assistant.stream_events import (
    HeartbeatOptions,
    engine_events_with_heartbeats,
)
from app.features.assistant.stream_persistence import (
    FinalOutcome,
    LeasePoll,
    StreamPersistence,
    StreamProgress,
    StreamWorker,
)
from app.features.workspace.permissions import (
    assert_can_write,
    assert_can_write_locked,
    assert_member,
    assert_member_locked,
)
from app.infra.datetime_utils import to_utc_datetime, utc_now
from app.infra.extensions import SessionFactory
from app.models.assistant import (
    AssistantConversation,
    AssistantMessage,
    AssistantRun,
)
from app.models.workspace import Workspace

LEASE_RENEW_SECONDS = 10.0
CHECKPOINT_EVERY = 20
CHECKPOINT_INTERVAL_SECONDS = 1.0
DEFAULT_GRACE_SECONDS = 45
MINIMUM_GRACE_SECONDS = 30
HEARTBEAT_FUTURE_TOLERANCE_SECONDS = 5


@dataclass(frozen=True, slots=True, kw_only=True)
class PrepareRunParams:
    """Durable inputs needed before opening one SSE run."""

    workspace: Workspace
    user_id: int
    conversation_id: int
    query: str
    request_id: str | None = None
    on_run_created: Callable[[tuple[int, int, int, int]], None] | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class UpdateConversationParams:
    """Partial update of one owned conversation."""

    workspace: Workspace
    user_id: int
    conversation_id: int
    title: str | None = None
    status: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class StreamPreparedRunParams:
    """Existing prepared Session whose owner keeps it idle."""

    session: Session
    workspace: Workspace
    user_id: int
    conversation: AssistantConversation
    run: AssistantRun
    history: list[dict[str, str]]


@dataclass(frozen=True, slots=True, kw_only=True)
class StreamRunParams:
    """Request identity for a stream that owns its Session."""

    session_factory: SessionFactory
    workspace_id: int
    user_id: int
    conversation_id: int
    run_id: int
    history: list[dict[str, str]]
    credential: StreamCredential | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class RespondPermissionParams:
    """One owner's one-shot permission response."""

    workspace: Workspace
    user_id: int
    run_id: int
    permission_id: str
    response: str
    remember: bool = False


def _grace_seconds() -> int:
    """Read the abandoned-run grace period with its historical floor."""
    try:
        return max(
            int(os.getenv("ASSISTANT_ABANDONED_RUN_GRACE_SECONDS", "45")),
            MINIMUM_GRACE_SECONDS,
        )
    except ValueError:
        return DEFAULT_GRACE_SECONDS


def _has_live_heartbeat(
    run: AssistantRun,
    now: datetime,
    grace_seconds: int,
) -> bool:
    """Return whether the durable worker heartbeat is still fresh."""
    heartbeat = run.metadata_dict.get("worker_heartbeat_unix")
    if (
        isinstance(heartbeat, (float, int))
        and not isinstance(heartbeat, bool)
        and math.isfinite(heartbeat)
    ):
        age = now.timestamp() - heartbeat
        if -HEARTBEAT_FUTURE_TOLERANCE_SECONDS <= age < grace_seconds:
            return True
    return False


def recover_abandoned_run(
    session: Session,
    run: AssistantRun,
    *,
    commit: bool = True,
) -> bool:
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
    locked = repository.lock_run(session, int(run.id))
    if locked is None or locked.status in repository.TERMINAL_RUN_STATUSES:
        return False
    run = locked
    started_at = to_utc_datetime(run.started_at or run.created_at)
    if started_at is None:
        return False
    grace_seconds = _grace_seconds()
    now: datetime | None = (
        session.scalar(select(func.clock_timestamp()))
        if session.get_bind().dialect.name == "postgresql"
        else utc_now()
    )
    if now is None:
        now = utc_now()
    if _has_live_heartbeat(run, now, grace_seconds):
        return False
    if (now - started_at).total_seconds() < grace_seconds:
        return False

    abort_repository.stage(session, run)
    assistant_message = session.get(AssistantMessage, run.assistant_message_id)
    if assistant_message is not None and assistant_message.status in {
        "pending",
        "streaming",
    }:
        assistant_message.status = "failed"
    run.pending_permission_id = None
    run.permission_response = None
    repository.update_run_status(
        run,
        "failed",
        error_code="stream_abandoned",
        error_message="助手请求在服务重启或连接中断后未完成,请重新发送。",
    )
    if commit:
        session.commit()
    else:
        session.flush()
    return True


def _conversation_or_404(
    session: Session,
    conversation_id: int,
    workspace_id: int,
    user_id: int,
) -> AssistantConversation:
    conversation = repository.get_conversation(
        session,
        conversation_id,
        workspace_id,
        user_id,
    )
    if conversation is None:
        message = "Assistant conversation not found"
        raise ResourceNotFoundError(message)
    return conversation


def create_conversation(
    session: Session,
    workspace: Workspace,
    user_id: int,
    mode: str,
    title: str | None = None,
) -> AssistantConversation:
    """Create one owned conversation after membership checks."""
    assert_member_locked(session, int(workspace.id), int(user_id))
    if mode == "expert":
        assert_can_write(session, int(workspace.id), int(user_id))
    conversation = repository.create_conversation(
        session,
        repository.CreateConversationParams(
            workspace_id=int(workspace.id),
            user_id=int(user_id),
            mode=mode,
            title=title,
        ),
    )
    session.commit()
    return conversation


def list_user_conversations(
    session: Session,
    workspace: Workspace,
    user_id: int,
    mode: str | None = None,
    *,
    include_archived: bool = False,
) -> list[AssistantConversation]:
    """List conversations the caller may see in this workspace."""
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
    """Return the live run of one conversation, recovering stale workers."""
    assert_member_locked(session, int(workspace.id), int(user_id))
    conversation = get_user_conversation(
        session,
        workspace,
        user_id,
        conversation_id,
    )
    run = repository.active_run_for_conversation(session, int(conversation.id))
    if run and recover_abandoned_run(session, run):
        return None
    return run


def get_active_expert_run_for_member(
    session: Session,
    workspace: Workspace,
    user_id: int,
) -> AssistantRun | None:
    """Return the member's live expert run, recovering stale workers."""
    assert_can_write_locked(session, int(workspace.id), int(user_id))
    run = repository.active_expert_run_for_member(
        session,
        int(workspace.id),
        int(user_id),
    )
    if run and recover_abandoned_run(session, run):
        return None
    return run


def get_user_conversation(
    session: Session,
    workspace: Workspace,
    user_id: int,
    conversation_id: int,
) -> AssistantConversation:
    """Return one conversation after membership and mode checks."""
    assert_member(session, int(workspace.id), int(user_id))
    conversation = _conversation_or_404(
        session,
        conversation_id,
        int(workspace.id),
        int(user_id),
    )
    if conversation.mode == "expert":
        assert_can_write(session, int(workspace.id), int(user_id))
    return conversation


def get_conversation_messages(
    session: Session,
    workspace: Workspace,
    user_id: int,
    conversation_id: int,
) -> list[AssistantMessage]:
    """Return the ordered messages of one owned conversation."""
    conversation = get_user_conversation(
        session,
        workspace,
        user_id,
        conversation_id,
    )
    return repository.get_messages(session, int(conversation.id))


def update_conversation(
    session: Session,
    params: UpdateConversationParams,
) -> AssistantConversation:
    """Rename or archive one owned conversation."""
    assert_member_locked(
        session,
        int(params.workspace.id),
        int(params.user_id),
    )
    get_user_conversation(
        session,
        params.workspace,
        params.user_id,
        params.conversation_id,
    )
    conversation = repository.lock_conversation(
        session,
        params.conversation_id,
        int(params.workspace.id),
        int(params.user_id),
    )
    if conversation is None:
        message = "Assistant conversation not found"
        raise ResourceNotFoundError(message)

    if params.title is not None:
        normalized_title = params.title.strip()
        if not normalized_title:
            message = "会话标题不能为空"
            raise BusinessRuleError(message)
        conversation.title = normalized_title

    if params.status is not None:
        if params.status not in {"active", "archived"}:
            message = "会话状态无效"
            raise BusinessRuleError(message)
        active_run = repository.active_run_for_conversation(
            session,
            int(conversation.id),
        )
        if active_run and recover_abandoned_run(
            session,
            active_run,
            commit=False,
        ):
            active_run = None
        if active_run and params.status == "archived":
            message = "当前会话仍有任务运行,请先停止任务"
            raise ConflictError(message)
        conversation.status = params.status

    session.commit()
    return conversation


def delete_conversation(
    session: Session,
    workspace: Workspace,
    user_id: int,
    conversation_id: int,
) -> None:
    """Delete one owned conversation that has no live run."""
    assert_member_locked(session, int(workspace.id), int(user_id))
    get_user_conversation(session, workspace, user_id, conversation_id)
    conversation = repository.lock_conversation(
        session,
        conversation_id,
        int(workspace.id),
        int(user_id),
    )
    if conversation is None:
        message = "Assistant conversation not found"
        raise ResourceNotFoundError(message)
    active_run = repository.active_run_for_conversation(
        session,
        int(conversation.id),
    )
    if active_run and recover_abandoned_run(session, active_run, commit=False):
        active_run = None
    if active_run:
        message = "当前会话仍有任务运行,请先停止任务"
        raise ConflictError(message)
    session.delete(conversation)
    session.commit()


def _prepare_conversation(
    session: Session,
    params: PrepareRunParams,
) -> AssistantConversation:
    """Lock the conversation and enforce the one-active-run rule."""
    assert_member_locked(session, int(params.workspace.id), int(params.user_id))
    get_user_conversation(
        session,
        params.workspace,
        params.user_id,
        params.conversation_id,
    )
    conversation = repository.lock_conversation(
        session,
        params.conversation_id,
        int(params.workspace.id),
        int(params.user_id),
    )
    if conversation is None:
        message = "Assistant conversation not found"
        raise ResourceNotFoundError(message)
    if conversation.status != "active":
        message = "会话已归档,请先恢复后再发送"
        raise ConflictError(message)
    if conversation.mode == "expert":
        assert_can_write(session, int(params.workspace.id), int(params.user_id))
        active_member_run = repository.active_expert_run_for_member(
            session,
            int(params.workspace.id),
            int(params.user_id),
        )
        if active_member_run and recover_abandoned_run(
            session,
            active_member_run,
            commit=False,
        ):
            active_member_run = None
        if active_member_run:
            message = "Expert runtime is already busy"
            raise ConflictError(message)

    active = repository.active_run_for_conversation(
        session,
        int(conversation.id),
    )
    if active and recover_abandoned_run(session, active, commit=False):
        active = None
    if active:
        message = "This assistant conversation already has a running task"
        raise ConflictError(message)

    if (
        conversation.mode == "expert"
        and abort_repository.pending_for_member(
            session,
            int(params.workspace.id),
            int(params.user_id),
        )
    ):
        message = "Expert runtime cleanup is still pending"
        raise ConflictError(message)
    return conversation


def prepare_run(
    session: Session,
    params: PrepareRunParams,
) -> tuple[AssistantConversation, AssistantRun, list[dict[str, str]]]:
    """Create the durable message/run records before opening the SSE stream."""
    conversation = _prepare_conversation(session, params)
    # The history excludes the new user message and pending assistant shell.
    history = [
        {"role": message.role, "content": message.content}
        for message in repository.get_messages(
            session,
            int(conversation.id),
            limit=20,
        )
        if message.status in {"completed", "failed", "cancelled"}
    ]
    user_message = repository.create_message(
        session,
        conversation,
        repository.CreateMessageParams(
            role="user",
            content=params.query.strip(),
            status="completed",
        ),
    )
    assistant_message = repository.create_message(
        session,
        conversation,
        repository.CreateMessageParams(
            role="assistant",
            content="",
            status="pending",
        ),
    )
    run = repository.create_run(
        session,
        conversation,
        user_message,
        assistant_message,
        request_id=params.request_id or str(uuid.uuid4()),
    )
    repository.update_run_status(run, "running")
    assistant_message.status = "streaming"
    if params.on_run_created is not None:
        # create_run has flushed its ID. Capture internal identity before commit
        # so a lost acknowledgement can be compensated in a fresh transaction.
        params.on_run_created(
            (
                int(params.workspace.id),
                int(params.user_id),
                int(conversation.id),
                int(run.id),
            ),
        )
    session.commit()
    return conversation, run, history


def _event_content(event: AssistantEvent) -> str:
    value = event.payload.get("content")
    return value if isinstance(value, str) else ""


def _annotate_event(event: AssistantEvent, run_id: int, message_id: int) -> None:
    """Attach durable identities without touching heartbeat payloads."""
    if event.run_id is None:
        event.run_id = run_id
    if event.message_id is None and event.type != "heartbeat":
        event.message_id = message_id


def _merge_event(progress: StreamProgress, event: AssistantEvent) -> None:
    """Fold one engine event into the persisted answer state."""
    if event.type == "token":
        progress.answer_parts.append(_event_content(event))
    elif event.type in {"title", "keywords"}:
        progress.metadata[event.type] = _event_content(event)
    elif event.type == "sources":
        progress.metadata["sources"] = event.payload.get("sources", [])
    elif event.type == "usage":
        progress.usage.update(event.payload)
    elif event.type == "error":
        message = str(event.payload.get("message") or "assistant engine error")
        raise RuntimeError(message)
    elif event.type == "file_diff":
        progress.metadata["diff"] = event.payload.get(
            "files",
            event.payload,
        )


@dataclass(slots=True)
class _StreamState:
    """Runtime collaborators for one persisted stream."""

    worker: StreamWorker
    persistence: StreamPersistence
    progress: StreamProgress
    controller: RunController
    run_id: int
    message_id: int
    stopped: bool = False


async def _finish(
    worker: StreamWorker,
    persistence: StreamPersistence,
    progress: StreamProgress,
    status: str,
    error_code: str | None = None,
) -> str | None:
    """Persist the terminal snapshot on the owning SQL worker."""
    outcome = FinalOutcome(
        status=status,
        content=progress.content,
        metadata=dict(progress.metadata),
        usage=dict(progress.usage),
        events=list(progress.event_log),
        error_code=error_code,
    )
    return await worker.call(persistence.finish, outcome)


async def _finish_cancelled(state: _StreamState) -> AssistantEvent | None:
    """Persist cancellation and return the event the client may see."""
    status = await _finish(
        state.worker,
        state.persistence,
        state.progress,
        "cancelled",
    )
    if status != "cancelled":
        return None
    return _status_event(state, "cancelled", status)


async def _finish_completed(state: _StreamState) -> AssistantEvent | None:
    """Persist completion and return the final status event."""
    status = await _finish(
        state.worker,
        state.persistence,
        state.progress,
        "completed",
    )
    if status not in {"completed", "cancelled"}:
        return None
    event_type = "cancelled" if status == "cancelled" else "done"
    return _status_event(state, event_type, status)


async def _finish_failed(
    state: _StreamState,
    error_code: str,
) -> AssistantEvent | None:
    """Persist failure and return the sanitized error event."""
    status = await _finish(
        state.worker,
        state.persistence,
        state.progress,
        "failed",
        error_code,
    )
    if status != "failed":
        return None
    return AssistantEvent(
        "error",
        {
            "code": error_code,
            "message": _stream_failure_message(state.persistence.mode),
        },
        run_id=state.run_id,
        message_id=state.message_id,
    )


def _status_event(
    state: _StreamState,
    event_type: str,
    status: str,
) -> AssistantEvent:
    return AssistantEvent(
        event_type,
        {"status": status},
        run_id=state.run_id,
        message_id=state.message_id,
    )


async def _poll_state(state: _StreamState) -> tuple[bool, bool]:
    """Refresh the lease at most every ten seconds and read run state."""
    progress = state.progress
    renew = time.monotonic() - progress.last_lease >= LEASE_RENEW_SECONDS
    terminal, cancelled = await state.worker.call(
        state.persistence.poll,
        LeasePoll(renew_lease=renew, now=time.time()),
    )
    if renew:
        progress.last_lease = time.monotonic()
    return terminal, cancelled


async def _poll_idle(state: _StreamState) -> None:
    """Authorize the idle producer without leaving checkpoints unflushed."""
    progress = state.progress
    renew = time.monotonic() - progress.last_lease >= LEASE_RENEW_SECONDS
    terminal, cancelled = await state.worker.call(
        state.persistence.poll,
        LeasePoll(renew_lease=renew, now=time.time(), idle=True),
    )
    if renew:
        progress.last_lease = time.monotonic()
    if terminal or cancelled or state.controller.cancel_event.is_set():
        message = "Assistant stream authorization ended"
        raise PermissionDeniedError(message)


async def _checkpoint_event(
    state: _StreamState,
    event: AssistantEvent,
) -> bool:
    """Persist one checkpoint and report whether the run is still current."""
    progress = state.progress
    progress.event_count += 1
    now = time.monotonic()
    commit = (
        progress.event_count % CHECKPOINT_EVERY == 0
        or now - progress.last_commit > CHECKPOINT_INTERVAL_SECONDS
    )
    permission = event.payload if event.type == "permission_required" else None
    if not await state.worker.call(
        state.persistence.checkpoint,
        progress,
        permission,
        commit=commit,
    ):
        return False
    if commit or permission is not None:
        progress.last_commit = time.monotonic()
    return True


def _stream_failure_message(mode: str) -> str:
    return "专家任务执行失败" if mode == "expert" else "快速问答执行失败"


async def _run_engine_events(
    state: _StreamState,
    request: EngineRequest,
) -> AsyncGenerator[AssistantEvent, None]:
    """Consume one engine stream while persisting its checkpoints."""
    worker = state.worker
    persistence = state.persistence
    progress = state.progress
    controller = state.controller
    engine = await worker.call(persistence.engine)
    iterator = engine.stream(request)
    async with aclosing(
        engine_events_with_heartbeats(
            iterator,
            state.run_id,
            HeartbeatOptions(
                on_idle=partial(_poll_idle, state),
                on_abort=controller.request_cancel,
            ),
        ),
    ) as engine_events:
        async for event in engine_events:
            terminal, cancelled = await _poll_state(state)
            if terminal:
                await controller.request_cancel()
                state.stopped = True
                return
            _annotate_event(event, state.run_id, state.message_id)
            if event.type != "heartbeat":
                progress.event_log.append(event)
            if (
                event.type == "cancelled"
                or controller.cancel_event.is_set()
                or cancelled
            ):
                await controller.request_cancel()
                final_event = await _finish_cancelled(state)
                if final_event is not None:
                    yield final_event
                state.stopped = True
                return
            _merge_event(progress, event)
            if not await _checkpoint_event(state, event):
                state.stopped = True
                return
            if event.type != "done":
                yield event


async def _stream_persisted(
    worker: StreamWorker,
    persistence: StreamPersistence,
    history: list[dict[str, str]],
) -> AsyncGenerator[AssistantEvent, None]:
    progress = StreamProgress()
    try:
        run_id, message_id, workspace_id, _mode, query = await worker.call(
            persistence.initialize,
            time.time(),
        )
    except asyncio.CancelledError:
        await _finish(worker, persistence, progress, "cancelled")
        raise
    except AuthenticationError:
        status = await _finish(worker, persistence, progress, "cancelled")
        if status == "cancelled":
            yield AssistantEvent(
                "cancelled",
                {"status": status},
                run_id=persistence.run_id,
                message_id=persistence.message_id,
            )
        return
    controller = run_registry.register(run_id)
    progress.last_commit = progress.last_lease = time.monotonic()
    state = _StreamState(
        worker=worker,
        persistence=persistence,
        progress=progress,
        controller=controller,
        run_id=run_id,
        message_id=message_id,
    )
    request = EngineRequest(
        run_id=run_id,
        user_id=persistence.user_id,
        workspace_id=workspace_id,
        query=query,
        history=history,
        controller=controller,
    )
    try:
        async with aclosing(_stream_with_state(state, request)) as events:
            async for event in events:
                yield event
    finally:
        run_registry.unregister(run_id)


async def _stream_with_state(
    state: _StreamState,
    request: EngineRequest,
) -> AsyncGenerator[AssistantEvent, None]:
    """Stream one engine run and persist every terminal outcome."""
    persistence = state.persistence
    progress = state.progress
    controller = state.controller
    try:
        yield AssistantEvent(
            "run_started",
            {"engine": persistence.mode, "status": "running"},
            run_id=state.run_id,
            message_id=state.message_id,
        )
        async with aclosing(_run_engine_events(state, request)) as events:
            async for event in events:
                yield event
        if state.stopped:
            return
        final_event = await _finish_completed(state)
        if final_event is not None:
            yield final_event
    except (asyncio.CancelledError, GeneratorExit):
        await _finish(state.worker, persistence, progress, "cancelled")
        raise
    except (AuthenticationError, PermissionDeniedError):
        if not controller.cancel_event.is_set():
            await controller.request_cancel()
        final_event = await _finish_cancelled(state)
        if final_event is not None:
            yield final_event
    except Exception as exc:
        final_event = await _finish_failed(
            state,
            exc.__class__.__name__,
        )
        if final_event is not None:
            yield final_event


def cleanup_stream_session(session: Session, operation: str) -> None:
    """Run cleanup on the owning worker without replacing the stream outcome."""
    try:
        getattr(session, operation)()
    except Exception as exc:
        logger.error(
            "Assistant stream session {} failed (error_type={})",
            operation,
            type(exc).__name__,
        )
        try:
            session.invalidate()
        except Exception as invalidation_exc:
            logger.error(
                "Assistant stream session invalidation failed (error_type={})",
                type(invalidation_exc).__name__,
            )


async def stream_prepared_run(
    params: StreamPreparedRunParams,
) -> AsyncGenerator[AssistantEvent, None]:
    """Compatibility seam for callers that own an existing prepared Session.

    Callers must leave that Session idle until this iterator closes. HTTP
    streams use stream_run, which creates and closes its Session in the worker.
    """
    async with StreamWorker() as worker:
        try:
            persistence = await worker.call(
                StreamPersistence,
                params.session,
                params.workspace,
                params.user_id,
                params.conversation,
                params.run,
            )
            async with aclosing(
                _stream_persisted(worker, persistence, params.history),
            ) as events:
                async for event in events:
                    yield event
        finally:
            await worker.call(
                cleanup_stream_session,
                params.session,
                "rollback",
            )


async def stream_run(
    params: StreamRunParams,
) -> AsyncGenerator[AssistantEvent, None]:
    """Own the streaming Session entirely within its dedicated worker thread."""
    async with StreamWorker() as worker:
        owned_sessions: list[Session] = []

        def create() -> StreamPersistence:
            session = params.session_factory()
            owned_sessions.append(session)
            workspace = session.get(Workspace, params.workspace_id)
            conversation = session.get(
                AssistantConversation,
                params.conversation_id,
            )
            run = session.get(AssistantRun, params.run_id)
            if workspace is None or conversation is None or run is None:
                message = "Assistant stream records disappeared"
                raise ResourceNotFoundError(message)
            persistence = StreamPersistence(
                session,
                workspace,
                params.user_id,
                conversation,
                run,
            )
            persistence.credential = params.credential
            return persistence

        try:
            persistence = await worker.call(create)
            async with aclosing(
                _stream_persisted(worker, persistence, params.history),
            ) as events:
                async for event in events:
                    yield event
        finally:
            for session in owned_sessions:
                await worker.call(cleanup_stream_session, session, "close")


def request_cancel(
    session: Session,
    workspace: Workspace,
    user_id: int,
    run_id: int,
) -> AssistantRun:
    """Persist cancellation and stage any required remote cleanup."""
    assert_member_locked(session, int(workspace.id), int(user_id))
    run = repository.get_owned_run(
        session,
        run_id,
        int(workspace.id),
        int(user_id),
    )
    if run is None:
        message = "Assistant run not found"
        raise ResourceNotFoundError(message)
    run = repository.lock_run(session, run_id)
    if run is None:
        message = "Assistant run not found"
        raise ResourceNotFoundError(message)
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
    params: RespondPermissionParams,
) -> AssistantRun:
    """Commit one one-shot permission response before waking the worker."""
    assert_member_locked(session, int(params.workspace.id), int(params.user_id))
    run = repository.get_owned_run(
        session,
        params.run_id,
        int(params.workspace.id),
        int(params.user_id),
    )
    if run is None:
        message = "Assistant run not found"
        raise ResourceNotFoundError(message)
    if run.engine == "expert":
        assert_can_write(session, int(params.workspace.id), int(params.user_id))
    if run.engine != "expert" or run.status != "waiting_permission":
        message = "This run is not waiting for a permission response"
        raise ConflictError(message)
    if run.pending_permission_id != params.permission_id:
        message = "Permission request does not belong to this run"
        raise PermissionDeniedError(message)
    # The provider receives only the one-shot response.  ``remember`` is
    # accepted for API compatibility but deliberately not persisted as a
    # permanent allow rule in the first version.
    # Persist first so a different API worker can wake the engine.  The local
    # future below is only the low-latency path for same-worker requests.
    if not repository.set_permission_response(
        session,
        params.run_id,
        params.permission_id,
        params.response,
    ):
        message = "Permission request has already been answered"
        raise ConflictError(message)
    session.commit()
    # A worker-thread notification can wake the stream immediately. Never
    # authorize provider execution before the durable transaction succeeds.
    run_registry.resolve_permission(
        params.run_id,
        params.permission_id,
        params.response,
    )
    return run


def handoff_to_expert(
    session: Session,
    workspace: Workspace,
    user_id: int,
    source_conversation_id: int,
    message_id: int | None = None,
) -> AssistantConversation:
    """Create the expert conversation that continues a fast conversation."""
    assert_can_write_locked(session, int(workspace.id), int(user_id))
    source = get_user_conversation(
        session,
        workspace,
        user_id,
        source_conversation_id,
    )
    if source.mode != "fast":
        message = "Only a fast conversation can be handed off"
        raise ConflictError(message)
    assert_can_write(session, int(workspace.id), int(user_id))

    messages = repository.get_messages(session, int(source.id), limit=10)
    if message_id is not None and (
        repository.get_message_for_conversation(
            session,
            message_id,
            int(source.id),
        )
        is None
    ):
        message = "Handoff message not found"
        raise ResourceNotFoundError(message)

    lines = [
        "以下内容来自 SKYCloud 快速问答,请在此基础上继续处理。",
        "",
    ]
    for history_message in messages:
        if (
            history_message.role in {"user", "assistant"}
            and history_message.content.strip()
        ):
            lines.append(
                f"{history_message.role}: {history_message.content.strip()}",
            )
        sources = history_message.metadata_dict.get("sources", [])
        if isinstance(sources, list):
            for source_item in sources[:8]:
                if isinstance(source_item, dict):
                    name = source_item.get("file_name") or source_item.get(
                        "file_id",
                    )
                    page = source_item.get("page_number")
                    if name:
                        page_suffix = f",第 {page} 页" if page else ""
                        lines.append(f"相关文件: {name}{page_suffix}")
    context = "\n".join(lines)
    target = repository.create_conversation(
        session,
        repository.CreateConversationParams(
            workspace_id=int(workspace.id),
            user_id=int(user_id),
            mode="expert",
            title=f"专家继续:{source.title or '快速问答'}",
            source_conversation_id=int(source.id),
        ),
    )
    repository.create_message(
        session,
        target,
        repository.CreateMessageParams(
            role="system",
            content=context,
            status="completed",
            metadata={
                "source_conversation_id": source.id,
                "handoff_message_id": message_id,
            },
        ),
    )
    session.commit()
    return target
