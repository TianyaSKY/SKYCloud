"""Transactional task staging and broker-confirmed, retryable delivery."""

from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy.orm import Session

from app.infra import task_queue
from app.infra.extensions import SessionFactory, SessionLocal
from app.models.file_task_outbox import FileTaskOutbox


def stage_file_tasks(session: Session, file_ids: Iterable[int]) -> None:
    """Add deliveries to the caller's transaction; never commit independently."""
    session.add_all([FileTaskOutbox(file_id=int(file_id)) for file_id in file_ids])


def deliver_pending_file_tasks(
    *,
    session_factory: SessionFactory = SessionLocal,
    limit: int = 20,
    publisher: Callable[[Iterable[int]], None] | None = None,
) -> int:
    """Lock one due row per transaction; an uncertain delivery can safely repeat."""
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
                # Best-effort delivery: the broker client raises its own types
                # and an uncertain publish must stay pending for a replay.
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
