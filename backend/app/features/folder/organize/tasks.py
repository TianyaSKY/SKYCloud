"""PostgreSQL 租约保护下的持久化目录整理任务投递与隔离执行."""

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
    """活跃执行者或瞬态数据库失败阻碍了当前处理, 需稍后重试."""


def _active_task(session: Session, workspace_id: int) -> OrganizeTask | None:
    """返回工作空间已准入的排队中或运行中的任务记录."""
    return (
        session.query(OrganizeTask)
        .filter(
            OrganizeTask.workspace_id == workspace_id,
            OrganizeTask.state.in_(["queued", "running"]),
        )
        .first()
    )


def admit_task(session: Session, workspace_id: int, user_id: int) -> bool:
    """按工作空间准入单个整理任务并持久化其投递记录."""
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
    """每笔事务投递一个到期的目录整理任务."""
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
                # 尽力投递: 消息代理客户端会抛出其专有异常类型,
                # 投递状态不确定的任务必须保持 pending 状态以便后续重试。
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
    """仅在当前执行上下文仍持有锁时返回锁定中的任务记录."""
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
    """在消息验证通过合法身份后返回对应的任务 ID."""
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
    """锁定或认领本条消息被允许执行的任务行记录."""
    task = (
        session.query(OrganizeTask)
        .filter_by(id=task_id)
        .with_for_update()
        .one_or_none()
    )
    if task is None:
        # 采纳具备合法主体凭据的可信历史 JSON 消息;
        # 绝不替换该工作空间下已准入的现有任务。
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
    """持有工作空间执行租约并产出本次执行令牌."""
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
                # 连接池回收的连接绝不能残留会话级 advisory lock。
                connection.invalidate()
