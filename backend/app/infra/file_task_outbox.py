"""Transactional task staging and broker-confirmed, retryable delivery."""
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy.orm import Session

from app.infra.extensions import SessionLocal
from app.infra import task_queue
from app.models.file_task_outbox import FileTaskOutbox


def stage_file_tasks(session: Session, file_ids) -> None:
    """Add deliveries to the caller's transaction; never commit independently."""
    session.add_all([FileTaskOutbox(file_id=int(file_id)) for file_id in file_ids])


def deliver_pending_file_tasks(*, session_factory=SessionLocal, limit=20, publisher=None) -> int:
    """Lock one due row per transaction; an uncertain delivery can safely repeat."""
    publisher = publisher or task_queue.publish_file_tasks
    delivered = 0
    for _ in range(limit):
        with session_factory() as session:
            now = datetime.now(timezone.utc)
            row = (
                session.query(FileTaskOutbox)
                .filter(FileTaskOutbox.delivered_at.is_(None),
                        FileTaskOutbox.next_attempt_at <= now)
                .order_by(FileTaskOutbox.id)
                .with_for_update(skip_locked=True)
                .first()
            )
            if row is None:
                break
            row.attempts += 1
            try:
                publisher([row.file_id])
            except Exception as error:
                row.last_error_type = type(error).__name__[:96]
                row.next_attempt_at = now + timedelta(seconds=min(300, 2 ** min(row.attempts, 9)))
                session.commit()
                logger.warning("File task {} delivery deferred ({})", row.id, row.last_error_type)
            else:
                row.delivered_at = datetime.now(timezone.utc)
                row.last_error_type = None
                session.commit()
                delivered += 1
    return delivered
