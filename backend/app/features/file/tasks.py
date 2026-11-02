"""事务性任务暂存以及经消息代理确认的幂等重试投递."""

from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy.orm import Session

from app.infra import task_queue
from app.infra.extensions import SessionFactory, SessionLocal
from app.models.file_task_outbox import FileTaskOutbox


def stage_file_tasks(session: Session, file_ids: Iterable[int]) -> None:
    """将待投递任务追加至调用方事务中; 绝不独立执行 commit."""
    session.add_all([FileTaskOutbox(file_id=int(file_id)) for file_id in file_ids])


def deliver_pending_file_tasks(
    *,
    session_factory: SessionFactory = SessionLocal,
    limit: int = 20,
    publisher: Callable[[Iterable[int]], None] | None = None,
) -> int:
    """每笔事务锁定一条到期记录; 投递状态不确定时可安全重复尝试."""
    publish = publisher or task_queue.publish_file_tasks
    delivered = 0
    for _ in range(limit):
        with session_factory() as session:
            now = datetime.now(UTC)
            row = (
                session.query(FileTaskOutbox)
                .filter(
                    FileTaskOutbox.delivered_at.is_(None),
                    FileTaskOutbox.next_attempt_at <= now,
                )
                .order_by(FileTaskOutbox.id)
                .with_for_update(skip_locked=True)
                .first()
            )
            if row is None:
                break
            row.attempts += 1
            try:
                publish([row.file_id])
            except Exception as error:
                # 尽力投递: 消息代理客户端会抛出其专有异常类型,
                # 投递状态不确定的任务必须保持 pending 状态以便后续重试。
                row.last_error_type = type(error).__name__[:96]
                row.next_attempt_at = now + timedelta(
                    seconds=min(300, 2 ** min(row.attempts, 9)),
                )
                session.commit()
                logger.warning(
                    "File task {} delivery deferred ({})",
                    row.id,
                    row.last_error_type,
                )
            else:
                row.delivered_at = datetime.now(UTC)
                row.last_error_type = None
                session.commit()
                delivered += 1
    return delivered
