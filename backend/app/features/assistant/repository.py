"""Database access for Assistant conversations, messages, and runs."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.exceptions import ConflictError
from app.features.assistant.event_protocol import AssistantEvent
from app.features.assistant.permission_details import permission_details
from app.features.workspace import runtime_operation_repository
from app.infra.datetime_utils import utc_now
from app.models.assistant import (
    AssistantConversation,
    AssistantMessage,
    AssistantRun,
)
from app.models.workspace import WorkspaceMember

if TYPE_CHECKING:
    from app.models.opencode_runtime import OpenCodeRuntime

TERMINAL_RUN_STATUSES = {"completed", "failed", "cancelled"}


@dataclass(frozen=True, slots=True, kw_only=True)
class CreateConversationParams:
    """Inputs for one owned assistant conversation."""

    workspace_id: int
    user_id: int
    mode: str
    title: str | None = None
    source_conversation_id: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class CreateMessageParams:
    """Inputs for one durable assistant message."""

    role: str
    content: str = ""
    status: str = "completed"
    metadata: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    provider_message_id: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class PermissionDispatchParams:
    """Identity of one permission response that a worker may dispatch."""

    workspace_id: int
    user_id: int
    conversation_id: int
    run_id: int
    runtime_id: int
    container_id: str
    provider_session_id: str
    permission_id: str
    response: str


def cancel_unclaimed_run(
    session: Session,
    workspace_id: int,
    user_id: int,
    conversation_id: int,
    run_id: int,
) -> bool:
    """Compensate a prepared HTTP run whose worker never acquired its lease."""
    run = (
        session.query(AssistantRun)
        .join(
            AssistantConversation,
            AssistantConversation.id == AssistantRun.conversation_id,
        )
        .filter(
            AssistantRun.id == run_id,
            AssistantRun.conversation_id == conversation_id,
            AssistantConversation.workspace_id == workspace_id,
            AssistantConversation.user_id == user_id,
        )
        .populate_existing()
        .with_for_update(of=AssistantRun)
        .first()
    )
    if (
        run is None
        or run.status in TERMINAL_RUN_STATUSES
        or "worker_heartbeat_unix" in run.metadata_dict
    ):
        return False
    message = session.get(AssistantMessage, run.assistant_message_id)
    if message is not None:
        message.status = "cancelled"
    run.cancel_requested = True
    update_run_status(run, "cancelled")
    return True


def get_conversation(
    session: Session,
    conversation_id: int,
    workspace_id: int,
    user_id: int,
) -> AssistantConversation | None:
    """Return one owned conversation."""
    return (
        session.query(AssistantConversation)
        .filter(
            AssistantConversation.id == conversation_id,
            AssistantConversation.workspace_id == workspace_id,
            AssistantConversation.user_id == user_id,
        )
        .first()
    )


def lock_conversation(
    session: Session,
    conversation_id: int,
    workspace_id: int,
    user_id: int,
) -> AssistantConversation | None:
    """Lock an owned conversation for the duration of a run preparation.

    The lock closes the check-then-insert window for message sequence numbers
    and the one-active-run rule.  SQLite ignores ``FOR UPDATE`` while
    PostgreSQL enforces it, which keeps the repository usable in the test
    fixture as well as in production.
    """
    return (
        session.query(AssistantConversation)
        .filter(
            AssistantConversation.id == conversation_id,
            AssistantConversation.workspace_id == workspace_id,
            AssistantConversation.user_id == user_id,
        )
        .populate_existing()
        .with_for_update()
        .first()
    )


def lock_workspace_member(
    session: Session,
    workspace_id: int,
    user_id: int,
) -> WorkspaceMember | None:
    """Serialize expert-run preparation for one workspace member."""
    return (
        session.query(WorkspaceMember)
        .filter(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == user_id,
        )
        .with_for_update()
        .first()
    )


def list_conversations(
    session: Session,
    workspace_id: int,
    user_id: int,
    mode: str | None = None,
    *,
    include_archived: bool = False,
) -> list[AssistantConversation]:
    """List conversations visible to one workspace member."""
    query = session.query(AssistantConversation).filter(
        AssistantConversation.workspace_id == workspace_id,
        AssistantConversation.user_id == user_id,
    )
    if not include_archived:
        query = query.filter(AssistantConversation.status == "active")
    if mode:
        query = query.filter(AssistantConversation.mode == mode)
    return query.order_by(
        AssistantConversation.last_message_at.desc().nullslast(),
        AssistantConversation.updated_at.desc(),
    ).all()


def create_conversation(
    session: Session,
    params: CreateConversationParams,
) -> AssistantConversation:
    """Insert one owned conversation and flush its identity."""
    conversation = AssistantConversation(
        workspace_id=params.workspace_id,
        user_id=params.user_id,
        mode=params.mode,
        title=(params.title or "").strip() or None,
        source_conversation_id=params.source_conversation_id,
    )
    session.add(conversation)
    session.flush()
    return conversation


def get_messages(
    session: Session,
    conversation_id: int,
    *,
    limit: int | None = None,
) -> list[AssistantMessage]:
    """Return messages in conversation order."""
    query = session.query(AssistantMessage).filter(
        AssistantMessage.conversation_id == conversation_id,
    )
    if limit:
        # Fetch the most recent messages while returning them in conversation order.
        return list(
            reversed(
                query.order_by(AssistantMessage.sequence.desc()).limit(limit).all(),
            ),
        )
    return query.order_by(AssistantMessage.sequence.asc()).all()


def create_message(
    session: Session,
    conversation: AssistantConversation,
    params: CreateMessageParams,
) -> AssistantMessage:
    """Insert one message with the conversation's next sequence number."""
    max_sequence = (
        session.query(func.max(AssistantMessage.sequence))
        .filter(AssistantMessage.conversation_id == conversation.id)
        .scalar()
    )
    message = AssistantMessage(
        conversation_id=conversation.id,
        role=params.role,
        content=params.content,
        status=params.status,
        provider_message_id=params.provider_message_id,
        sequence=int(max_sequence or 0) + 1,
    )
    message.metadata_dict = params.metadata or {}
    message.usage = params.usage
    session.add(message)
    conversation.last_message_at = utc_now()
    conversation.updated_at = utc_now()
    session.flush()
    return message


def create_run(
    session: Session,
    conversation: AssistantConversation,
    user_message: AssistantMessage,
    assistant_message: AssistantMessage,
    *,
    request_id: str | None = None,
) -> AssistantRun:
    """Insert the pending run that owns one streaming exchange."""
    run = AssistantRun(
        conversation_id=conversation.id,
        user_message_id=user_message.id,
        assistant_message_id=assistant_message.id,
        engine=conversation.mode,
        status="pending",
        request_id=request_id,
    )
    session.add(run)
    session.flush()
    return run


def lock_run(session: Session, run_id: int) -> AssistantRun | None:
    """Lock and refresh one run row."""
    return (
        session.query(AssistantRun)
        .filter(AssistantRun.id == run_id)
        .populate_existing()
        .with_for_update()
        .first()
    )


def touch_run_lease(session: Session, run_id: int, now: float) -> bool:
    """Refresh the worker heartbeat under the run row lock.

    The caller owns the transaction and must commit (or roll back) to release
    the row lock; an uncommitted heartbeat must never look renewed to another
    worker.
    """
    run = lock_run(session, run_id)
    if run is None or run.status in TERMINAL_RUN_STATUSES:
        return False
    run.metadata_dict = {**run.metadata_dict, "worker_heartbeat_unix": now}
    session.flush()
    return True


def set_pending_permission(run: AssistantRun, payload: dict[str, Any]) -> None:
    """Persist the bounded permission prompt and enter the waiting state."""
    permission_id = payload.get("permission_id")
    details = permission_details(
        {**payload, "title": payload.get("title") or "专家请求执行高风险操作"},
        permission_id,
    )
    if details is None:
        message = "Invalid permission display contract"
        raise ValueError(message)
    run.pending_permission_id = permission_id
    run.permission_response = None
    run.metadata_dict = {**run.metadata_dict, "pending_permission": details}
    update_run_status(run, "waiting_permission")


def clear_pending_permission(run: AssistantRun) -> None:
    """Remove any pending prompt from the run."""
    run.pending_permission_id = None
    run.permission_response = None
    metadata = run.metadata_dict
    metadata.pop("pending_permission", None)
    run.metadata_dict = metadata


def update_run_status(
    run: AssistantRun,
    status: str,
    *,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    """Apply one status transition and its terminal bookkeeping."""
    run.status = status
    if status == "running" and run.started_at is None:
        run.started_at = utc_now()
    if status in TERMINAL_RUN_STATUSES:
        clear_pending_permission(run)
        run.completed_at = utc_now()
    if error_code is not None:
        run.error_code = error_code
    if error_message is not None:
        run.error_message = error_message[:4000]


def active_run_for_conversation(
    session: Session,
    conversation_id: int,
) -> AssistantRun | None:
    """Return the newest non-terminal run of one conversation."""
    return (
        session.query(AssistantRun)
        .filter(
            AssistantRun.conversation_id == conversation_id,
            AssistantRun.status.notin_(list(TERMINAL_RUN_STATUSES)),
        )
        .order_by(AssistantRun.created_at.desc())
        .first()
    )


def get_owned_run(
    session: Session,
    run_id: int,
    workspace_id: int,
    user_id: int,
) -> AssistantRun | None:
    """Return one run owned through its conversation."""
    return (
        session.query(AssistantRun)
        .join(
            AssistantConversation,
            AssistantConversation.id == AssistantRun.conversation_id,
        )
        .filter(
            AssistantRun.id == run_id,
            AssistantConversation.workspace_id == workspace_id,
            AssistantConversation.user_id == user_id,
        )
        .first()
    )


def active_expert_run_for_member(
    session: Session,
    workspace_id: int,
    user_id: int,
) -> AssistantRun | None:
    """Return the member's live expert run; one runtime per member/space."""
    return (
        session.query(AssistantRun)
        .join(
            AssistantConversation,
            AssistantConversation.id == AssistantRun.conversation_id,
        )
        .filter(
            AssistantConversation.workspace_id == workspace_id,
            AssistantConversation.user_id == user_id,
            AssistantConversation.mode == "expert",
            AssistantRun.status.notin_(list(TERMINAL_RUN_STATUSES)),
        )
        .order_by(AssistantRun.created_at.desc())
        .first()
    )


def is_run_cancel_requested(session: Session, run_id: int) -> bool:
    """Read the durable cancel flag without relying on a stale ORM instance."""
    value = (
        session.query(AssistantRun.cancel_requested)
        .filter(AssistantRun.id == run_id)
        .scalar()
    )
    return bool(value)


def get_permission_response(
    session: Session,
    run_id: int,
    permission_id: str,
) -> str | None:
    """Read a one-shot response written by any API worker."""
    row = (
        session.query(
            AssistantRun.pending_permission_id,
            AssistantRun.permission_response,
        )
        .filter(AssistantRun.id == run_id)
        .first()
    )
    if row is None or row[0] != permission_id:
        return None
    return str(row[1]) if row[1] else None


def set_permission_response(
    session: Session,
    run_id: int,
    permission_id: str,
    response: str,
) -> bool:
    """Atomically claim a pending permission response exactly once."""
    updated = (
        session.query(AssistantRun)
        .filter(
            AssistantRun.id == run_id,
            AssistantRun.status == "waiting_permission",
            AssistantRun.pending_permission_id == permission_id,
            AssistantRun.permission_response.is_(None),
        )
        .update(
            {"permission_response": response, "status": "running"},
            synchronize_session="fetch",
        )
    )
    return bool(updated)


def get_message_for_conversation(
    session: Session,
    message_id: int,
    conversation_id: int,
) -> AssistantMessage | None:
    """Return a message only when it belongs to the given conversation."""
    return (
        session.query(AssistantMessage)
        .filter(
            AssistantMessage.id == message_id,
            AssistantMessage.conversation_id == conversation_id,
        )
        .first()
    )


def serialize_events_metadata(events: list[AssistantEvent]) -> dict[str, Any]:
    """Keep a small, safe summary rather than persisting provider payloads."""
    tools: list[dict[str, Any]] = []
    for event in events:
        if event.type not in {"tool_started", "tool_updated", "tool_completed"}:
            continue
        payload = event.payload
        tool_id = str(payload.get("tool_call_id") or payload.get("id") or "")
        existing = next(
            (item for item in tools if item.get("id") == tool_id),
            None,
        )
        if existing is None:
            existing = {
                "id": tool_id,
                "name": payload.get("tool"),
                "status": event.type,
            }
            tools.append(existing)
        existing["status"] = payload.get("status") or event.type.removeprefix(
            "tool_",
        )
    return {"tool_calls": tools} if tools else {}


def lock_permission_dispatch(
    session: Session,
    params: PermissionDispatchParams,
) -> AssistantRun:
    """Lock the runtime, run, and conversation a permission response targets.

    Caller keeps lifecycle/member/run locks until bounded dispatch completes.
    """
    runtime: OpenCodeRuntime = runtime_operation_repository.current_runtime(
        session,
        runtime_operation_repository.CurrentRuntimeParams(
            workspace_id=params.workspace_id,
            user_id=params.user_id,
            runtime_id=params.runtime_id,
            container_id=params.container_id,
        ),
    )
    run = lock_run(session, params.run_id)
    conversation = get_conversation(
        session,
        params.conversation_id,
        params.workspace_id,
        params.user_id,
    )
    if (
        runtime.status != "running"
        or conversation is None
        or run is None
        or run.conversation_id != params.conversation_id
        or run.engine != "expert"
        or run.status in TERMINAL_RUN_STATUSES
        or run.cancel_requested
        or conversation.opencode_runtime_id != params.runtime_id
        or conversation.opencode_session_id != params.provider_session_id
        or run.opencode_session_id != params.provider_session_id
        or run.pending_permission_id != params.permission_id
        or run.permission_response != params.response
        or params.response not in {"once", "reject"}
    ):
        message = "Permission dispatch is no longer current"
        raise ConflictError(message)
    return run
