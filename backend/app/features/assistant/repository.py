"""Database access for Assistant conversations, messages, and runs."""

from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.features.assistant.event_protocol import AssistantEvent
from app.infra.datetime_utils import beijing_now
from app.models.assistant import AssistantConversation, AssistantMessage, AssistantRun
from app.models.workspace import WorkspaceMember


TERMINAL_RUN_STATUSES = {"completed", "failed", "cancelled"}


def get_conversation(
    session: Session,
    conversation_id: int,
    workspace_id: int,
    user_id: int,
) -> AssistantConversation | None:
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
    include_archived: bool = False,
) -> list[AssistantConversation]:
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
    workspace_id: int,
    user_id: int,
    mode: str,
    title: str | None = None,
    source_conversation_id: int | None = None,
) -> AssistantConversation:
    conversation = AssistantConversation(
        workspace_id=workspace_id,
        user_id=user_id,
        mode=mode,
        title=(title or "").strip() or None,
        source_conversation_id=source_conversation_id,
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
    query = session.query(AssistantMessage).filter(
        AssistantMessage.conversation_id == conversation_id
    )
    if limit:
        # Fetch the most recent messages while returning them in conversation order.
        return list(reversed(query.order_by(AssistantMessage.sequence.desc()).limit(limit).all()))
    return query.order_by(AssistantMessage.sequence.asc()).all()


def create_message(
    session: Session,
    conversation: AssistantConversation,
    role: str,
    content: str = "",
    *,
    status: str = "completed",
    metadata: dict[str, Any] | None = None,
    usage: dict[str, Any] | None = None,
    provider_message_id: str | None = None,
) -> AssistantMessage:
    max_sequence = (
        session.query(func.max(AssistantMessage.sequence))
        .filter(AssistantMessage.conversation_id == conversation.id)
        .scalar()
    )
    message = AssistantMessage(
        conversation_id=conversation.id,
        role=role,
        content=content,
        status=status,
        provider_message_id=provider_message_id,
        sequence=int(max_sequence or 0) + 1,
    )
    message.metadata_dict = metadata or {}
    message.usage = usage
    session.add(message)
    conversation.last_message_at = beijing_now()
    conversation.updated_at = beijing_now()
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


def update_run_status(
    run: AssistantRun,
    status: str,
    *,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    run.status = status
    if status == "running" and run.started_at is None:
        run.started_at = beijing_now()
    if status in TERMINAL_RUN_STATUSES:
        run.completed_at = beijing_now()
    if error_code is not None:
        run.error_code = error_code
    if error_message is not None:
        run.error_message = error_message[:4000]


def active_run_for_conversation(
    session: Session, conversation_id: int
) -> AssistantRun | None:
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
    return (
        session.query(AssistantRun)
        .join(AssistantConversation, AssistantConversation.id == AssistantRun.conversation_id)
        .filter(
            AssistantRun.id == run_id,
            AssistantConversation.workspace_id == workspace_id,
            AssistantConversation.user_id == user_id,
        )
        .first()
    )


def active_expert_run_for_member(
    session: Session, workspace_id: int, user_id: int
) -> AssistantRun | None:
    """The current product maps one OpenCode Runtime to one member/space."""

    return (
        session.query(AssistantRun)
        .join(AssistantConversation, AssistantConversation.id == AssistantRun.conversation_id)
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
        session.query(AssistantRun.pending_permission_id, AssistantRun.permission_response)
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
        existing = next((item for item in tools if item.get("id") == tool_id), None)
        if existing is None:
            existing = {"id": tool_id, "name": payload.get("tool"), "status": event.type}
            tools.append(existing)
        existing["status"] = payload.get("status") or event.type.removeprefix("tool_")
    return {"tool_calls": tools} if tools else {}
