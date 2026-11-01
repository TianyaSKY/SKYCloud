"""Database access for Assistant conversations, messages, and runs."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

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
    AssistantPermission,
    AssistantRun,
)
from app.models.workspace import WorkspaceMember

if TYPE_CHECKING:
    from runtime_common.json_types import JsonValue

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
    update_run_status(session, run, "cancelled")
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


def register_permission(
    session: Session,
    run: AssistantRun,
    payload: Mapping[str, object],
) -> AssistantPermission:
    """Persist one provider request without replacing an existing decision."""
    locked = lock_run(session, run.id)
    if locked is None:
        message = "Assistant run is no longer available"
        raise ConflictError(message)

    permission_id = payload.get("permission_id")
    existing = (
        permission_for_run(session, locked.id, permission_id)
        if isinstance(permission_id, str)
        else None
    )
    if existing is not None:
        session.expire(locked, ["current_permission"])
        return existing
    details_payload = dict(payload)
    details_payload["title"] = (
        details_payload.get("title") or "专家请求执行高风险操作"
    )
    details = permission_details(
        cast("JsonValue", details_payload),
        permission_id if isinstance(permission_id, str) else None,
    )
    if details is None:
        message = "Invalid permission display contract"
        raise ValueError(message)
    if locked.status in TERMINAL_RUN_STATUSES or locked.cancel_requested:
        message = "Assistant run is no longer active"
        raise ConflictError(message)
    open_permission = (
        session.query(AssistantPermission)
        .filter(
            AssistantPermission.run_id == locked.id,
            AssistantPermission.state.in_(("pending", "answered")),
        )
        .populate_existing()
        .first()
    )
    if open_permission is not None:
        message = "Another permission request is still open"
        raise ConflictError(message)

    permission = AssistantPermission(
        run_id=locked.id,
        permission_id=details["permission_id"],
        state="pending",
        details_json=json.dumps(
            details,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    )
    session.add(permission)
    update_run_status(session, locked, "waiting_permission")
    session.flush()
    session.expire(locked, ["current_permission"])
    return permission


def permission_for_run(
    session: Session,
    run_id: int,
    permission_id: str,
) -> AssistantPermission | None:
    """Read and refresh one permission record owned by a run."""
    return (
        session.query(AssistantPermission)
        .filter(
            AssistantPermission.run_id == run_id,
            AssistantPermission.permission_id == permission_id,
        )
        .populate_existing()
        .first()
    )


def answer_permission(
    session: Session,
    run: AssistantRun,
    permission_id: str,
    response: Literal["once", "reject"],
) -> bool:
    """Claim the pending permission once while its run is still waiting."""
    locked = lock_run(session, run.id)
    if (
        locked is None
        or locked.status != "waiting_permission"
        or locked.cancel_requested
        or locked.status in TERMINAL_RUN_STATUSES
        or response not in {"once", "reject"}
    ):
        return False
    permission = permission_for_run(session, locked.id, permission_id)
    if permission is None or permission.state != "pending":
        return False
    updated = (
        session.query(AssistantPermission)
        .filter(
            AssistantPermission.run_id == locked.id,
            AssistantPermission.permission_id == permission_id,
            AssistantPermission.state == "pending",
        )
        .update(
            {
                "state": "answered",
                "response": response,
                "answered_at": utc_now(),
            },
            synchronize_session="fetch",
        )
    )
    if not updated:
        return False
    update_run_status(session, locked, "running")
    session.flush()
    session.expire(locked, ["current_permission"])
    return True


def mark_permission_dispatched(
    session: Session,
    run: AssistantRun,
    permission: AssistantPermission,
) -> None:
    """Record provider acknowledgment without deleting the decision."""
    if (
        permission.run_id != run.id
        or permission.state != "answered"
        or permission.response not in {"once", "reject"}
        or run.status in TERMINAL_RUN_STATUSES
        or run.cancel_requested
    ):
        message = "Permission response is no longer dispatchable"
        raise ConflictError(message)
    permission.state = "dispatched"
    permission.dispatched_at = utc_now()
    update_run_status(session, run, "running")
    session.flush()
    session.expire(run, ["current_permission"])


def cancel_open_permissions(session: Session, run_id: int) -> None:
    """Close pending or answered decisions without erasing their response."""
    session.query(AssistantPermission).filter(
        AssistantPermission.run_id == run_id,
        AssistantPermission.state.in_(("pending", "answered")),
    ).update(
        {"state": "cancelled"},
        synchronize_session="fetch",
    )


def update_run_status(
    session: Session,
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
        cancel_open_permissions(session, run.id)
        run.completed_at = utc_now()
    if error_code is not None:
        run.error_code = error_code
    if error_message is not None:
        run.error_message = error_message[:4000]
    session.expire(run, ["current_permission"])



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
    permission = (
        permission_for_run(session, run.id, params.permission_id)
        if run is not None
        else None
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
        or permission is None
        or permission.state != "answered"
        or permission.response != params.response
        or params.response not in {"once", "reject"}
    ):
        message = "Permission dispatch is no longer current"
        raise ConflictError(message)
    return run
