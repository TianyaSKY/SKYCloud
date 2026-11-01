"""Durable organizer delivery and fenced execution under a PostgreSQL lease."""

import uuid
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy import Connection, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.exceptions import PermissionDeniedError
from app.features.workspace.permissions import assert_can_write
from app.infra import task_queue
from app.infra.extensions import SessionFactory, SessionLocal
from app.models.organize_task import OrganizeTask
from app.models.workspace import Workspace

LOCK_NAMESPACE = 731948212
TASK_ID_MAX_LENGTH = 64
MAX_IDENTIFIER = 2147483647
TERMINAL_TASK_STATES = ("completed", "failed")


class RetryTaskLater(Exception):
    """A live executor or a transient database failure prevents processing now."""


def _active_task(session: Session, workspace_id: int) -> OrganizeTask | None:
    """Return the queued or running task admitted for one workspace."""
    return (
        session.query(OrganizeTask)
        .filter(
            OrganizeTask.workspace_id == workspace_id,
            OrganizeTask.state.in_(["queued", "running"]),
        )
        .first()
    )


def admit_task(session: Session, workspace_id: int, user_id: int) -> bool:
    """Admit one organizer task per workspace and commit its delivery row."""
    workspace = (
        session.query(Workspace)
        .filter_by(id=workspace_id)
        .with_for_update()
        .one_or_none()
    )
    if workspace is None:
        message = "Workspace does not exist"
        raise PermissionDeniedError(message)
    assert_can_write(session, workspace_id, user_id)
    if _active_task(session, workspace_id) is not None:
        return False
    session.add(
        OrganizeTask(id=str(uuid.uuid4()), workspace_id=workspace_id, user_id=user_id),
    )
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise
    return True


def deliver_pending_organize_tasks(
    *,
    session_factory: SessionFactory = SessionLocal,
    publisher: Callable[[int, int, str], None] | None = None,
    limit: int = 20,
) -> int:
    """Deliver one due organizer task per transaction."""
    publish = publisher or task_queue.publish_organize_task
    delivered = 0
    for _ in range(limit):
        with session_factory() as session:
            now = datetime.now(UTC)
            row = (
                session.query(OrganizeTask)
                .filter(
                    OrganizeTask.delivered_at.is_(None),
                    OrganizeTask.state.in_(["queued", "running"]),
                    OrganizeTask.next_attempt_at <= now,
                )
                .order_by(OrganizeTask.created_at, OrganizeTask.id)
                .with_for_update(skip_locked=True)
                .first()
            )
            if row is None:
                break
            row.attempts += 1
            try:
                publish(row.workspace_id, row.user_id, row.id)
            except Exception as error:
                # Best-effort delivery: the broker client raises its own types
                # and an uncertain publish must stay pending for a replay.
                row.last_error_type = type(error).__name__[:96]
                row.next_attempt_at = now + timedelta(
                    seconds=min(300, 2 ** min(row.attempts, 9)),
                )
                session.commit()
                logger.warning(
                    "Organize delivery {} deferred ({})",
                    row.id,
                    row.last_error_type,
                )
            else:
                row.delivered_at = datetime.now(UTC)
                row.last_error_type = None
                session.commit()
                delivered += 1
    return delivered


def assert_task_claim(
    session: Session,
    task_id: str | None,
    execution_token: str | None,
    workspace_id: int,
    user_id: int,
) -> OrganizeTask:
    """Return the locked task only while this execution still owns it."""
    task = (
        session.query(OrganizeTask)
        .filter_by(id=task_id)
        .with_for_update()
        .one_or_none()
    )
    if (
        task is None
        or task.state != "running"
        or task.execution_token != execution_token
        or task.workspace_id != workspace_id
        or task.user_id != user_id
    ):
        message = "Organize execution no longer owns its task"
        raise PermissionDeniedError(message)
    return task


def _require_message_identity(
    workspace_id: int,
    user_id: int,
    task_id: str | None,
) -> str:
    """Return the task id once the message proves a valid identity."""
    if not isinstance(task_id, str):
        message = "Organize message requires task and user identity"
        raise PermissionDeniedError(message)
    if not (
        1 <= len(task_id) <= TASK_ID_MAX_LENGTH
        and 1 <= workspace_id <= MAX_IDENTIFIER
        and 1 <= user_id <= MAX_IDENTIFIER
    ):
        message = "Organize message requires task and user identity"
        raise PermissionDeniedError(message)
    return task_id


def _claim_task(
    session: Session,
    workspace_id: int,
    user_id: int,
    task_id: str,
) -> OrganizeTask:
    """Lock or adopt the task row this message is allowed to execute."""
    task = (
        session.query(OrganizeTask)
        .filter_by(id=task_id)
        .with_for_update()
        .one_or_none()
    )
    if task is None:
        # Adopt trusted legacy JSON messages with a valid principal;
        # never replace an already admitted task for this workspace.
        workspace = (
            session.query(Workspace)
            .filter_by(id=workspace_id)
            .with_for_update()
            .one_or_none()
        )
        if workspace is None:
            message = "Workspace does not exist"
            raise PermissionDeniedError(message)
        assert_can_write(session, workspace_id, user_id)
        if _active_task(session, workspace_id) is not None:
            message = "Legacy task conflicts with an admitted task"
            raise PermissionDeniedError(message)
        task = OrganizeTask(id=task_id, workspace_id=workspace_id, user_id=user_id)
        session.add(task)
    if task.workspace_id != workspace_id or task.user_id != user_id:
        message = "Organize message identity differs from its task"
        raise PermissionDeniedError(message)
    return task


@contextmanager
def task_execution(
    workspace_id: int,
    user_id: int,
    task_id: str | None,
    *,
    session_factory: SessionFactory = SessionLocal,
) -> Generator[str | None, None, None]:
    """Hold the workspace lease and yield this execution token."""
    valid_task_id = _require_message_identity(workspace_id, user_id, task_id)
    with session_factory() as session:
        bind = session.get_bind()
    engine = bind.engine if isinstance(bind, Connection) else bind
    with engine.connect() as connection:
        if not connection.scalar(
            text("SELECT pg_try_advisory_lock(:namespace,:workspace)"),
            {"namespace": LOCK_NAMESPACE, "workspace": workspace_id},
        ):
            message = "Another organizer holds the workspace lease"
            raise RetryTaskLater(message)
        connection.commit()
        try:
            with session_factory() as session:
                task = _claim_task(session, workspace_id, user_id, valid_task_id)
                if task.state in TERMINAL_TASK_STATES:
                    claim = None
                else:
                    claim = str(uuid.uuid4())
                    task.state = "running"
                    task.execution_token = claim
                    session.commit()
            yield claim
        finally:
            try:
                connection.execute(
                    text("SELECT pg_advisory_unlock(:namespace,:workspace)"),
                    {"namespace": LOCK_NAMESPACE, "workspace": workspace_id},
                )
                connection.commit()
            except SQLAlchemyError:
                # A pooled connection must never retain a session advisory lock.
                connection.invalidate()
