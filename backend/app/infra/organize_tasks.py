"""Durable organizer delivery and fenced execution under a PostgreSQL lease."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import uuid

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from loguru import logger

from app.exceptions import PermissionDeniedError
from app.infra.extensions import SessionLocal
from app.infra import task_queue
from app.models.organize_task import OrganizeTask
from app.models.workspace import Workspace
from app.features.workspace.permissions import assert_can_write

LOCK_NAMESPACE = 731948212


class RetryTaskLater(Exception):
    """A live executor or a transient database failure prevents processing now."""


def admit_task(session, workspace_id: int, user_id: int) -> bool:
    workspace = session.query(Workspace).filter_by(id=workspace_id).with_for_update().one_or_none()
    if workspace is None:
        raise PermissionDeniedError("Workspace does not exist")
    assert_can_write(session, workspace_id, user_id)
    active = session.query(OrganizeTask).filter(
        OrganizeTask.workspace_id == workspace_id,
        OrganizeTask.state.in_(["queued", "running"]),
    ).first()
    if active is not None:
        return False
    session.add(OrganizeTask(id=str(uuid.uuid4()), workspace_id=workspace_id, user_id=user_id))
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise
    return True


def deliver_pending_organize_tasks(*, session_factory=SessionLocal, publisher=None, limit=20):
    publisher = publisher or task_queue.publish_organize_task
    delivered = 0
    for _ in range(limit):
        with session_factory() as session:
            now = datetime.now(timezone.utc)
            row = session.query(OrganizeTask).filter(
                OrganizeTask.delivered_at.is_(None),
                OrganizeTask.state.in_(["queued", "running"]),
                OrganizeTask.next_attempt_at <= now,
            ).order_by(OrganizeTask.created_at, OrganizeTask.id).with_for_update(skip_locked=True).first()
            if row is None:
                break
            row.attempts += 1
            try:
                publisher(row.workspace_id, row.user_id, row.id)
            except Exception as error:
                row.last_error_type = type(error).__name__[:96]
                row.next_attempt_at = now + timedelta(seconds=min(300, 2 ** min(row.attempts, 9)))
                session.commit()
                logger.warning("Organize delivery {} deferred ({})", row.id, row.last_error_type)
            else:
                row.delivered_at = datetime.now(timezone.utc)
                row.last_error_type = None
                session.commit()
                delivered += 1
    return delivered


def assert_task_claim(session, task_id, execution_token, workspace_id, user_id):
    task = session.query(OrganizeTask).filter_by(id=task_id).with_for_update().one_or_none()
    if (task is None or task.state != "running" or task.execution_token != execution_token
            or task.workspace_id != workspace_id or task.user_id != user_id):
        raise PermissionDeniedError("Organize execution no longer owns its task")
    return task


@contextmanager
def task_execution(workspace_id, user_id, task_id, *, session_factory=SessionLocal):
    if (not isinstance(task_id, str) or not 1 <= len(task_id) <= 64
            or not 1 <= workspace_id <= 2147483647 or not 1 <= user_id <= 2147483647):
        raise PermissionDeniedError("Organize message requires task and user identity")
    with session_factory() as session:
        bind = session.get_bind()
        engine = getattr(bind, "engine", bind)
    with engine.connect() as connection:
        if not connection.scalar(text("SELECT pg_try_advisory_lock(:namespace,:workspace)"),
                                 {"namespace": LOCK_NAMESPACE, "workspace": workspace_id}):
            raise RetryTaskLater("Another organizer holds the workspace lease")
        connection.commit()
        try:
            with session_factory() as session:
                task = session.query(OrganizeTask).filter_by(id=task_id).with_for_update().one_or_none()
                if task is None:
                    # Adopt trusted legacy JSON messages with a valid principal;
                    # never replace an already admitted task for this workspace.
                    workspace = session.query(Workspace).filter_by(id=workspace_id).with_for_update().one_or_none()
                    if workspace is None:
                        raise PermissionDeniedError("Workspace does not exist")
                    assert_can_write(session, workspace_id, user_id)
                    active = session.query(OrganizeTask).filter(
                        OrganizeTask.workspace_id == workspace_id,
                        OrganizeTask.state.in_(["queued", "running"]),
                    ).first()
                    if active is not None:
                        raise PermissionDeniedError("Legacy task conflicts with an admitted task")
                    task = OrganizeTask(id=task_id, workspace_id=workspace_id, user_id=user_id)
                    session.add(task)
                if task.workspace_id != workspace_id or task.user_id != user_id:
                    raise PermissionDeniedError("Organize message identity differs from its task")
                if task.state in ("completed", "failed"):
                    claim = None
                else:
                    claim = str(uuid.uuid4())
                    task.state = "running"
                    task.execution_token = claim
                    session.commit()
            yield claim
        finally:
            try:
                connection.execute(text("SELECT pg_advisory_unlock(:namespace,:workspace)"),
                                   {"namespace": LOCK_NAMESPACE, "workspace": workspace_id})
                connection.commit()
            except SQLAlchemyError:
                # A pooled connection must never retain a session advisory lock.
                connection.invalidate()
