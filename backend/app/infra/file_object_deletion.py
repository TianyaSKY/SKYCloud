"""Durable, reference-checked object cleanup with instant-clone serialization."""
from datetime import timedelta
import hashlib

from loguru import logger
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.infra.datetime_utils import utc_now
from app.infra.extensions import SessionLocal
from app.infra.storage import get_storage_client
from app.models.file import File
from app.models.file_object_deletion import FileObjectDeletion


def lock_object_path(session: Session, object_name: str) -> None:
    """PG transaction mutex shared by cleaner and instant-clone admission.

    Hash collisions only serialize unrelated keys. SQLite is a single-writer
    test seam; deployment migrations require PostgreSQL.
    """
    if session.get_bind().dialect.name == "postgresql":
        key = int.from_bytes(hashlib.sha256(object_name.encode()).digest()[:4], "big", signed=True)
        session.execute(text("SELECT pg_advisory_xact_lock(731948214, :key)"), {"key": key})


def stage_object_deletion(session: Session, object_name: str) -> None:
    session.add(FileObjectDeletion(object_name=object_name))


def delete_pending_objects(*, session_factory=SessionLocal, storage_factory=get_storage_client,
                           limit: int = 20) -> int:
    """Retry idempotent deletes; committed references always protect the object."""
    completed = 0
    for _ in range(limit):
        with session_factory() as session:
            now = utc_now()
            intent = session.query(FileObjectDeletion).filter(
                FileObjectDeletion.completed_at.is_(None), FileObjectDeletion.next_attempt_at <= now
            ).order_by(FileObjectDeletion.id).with_for_update(skip_locked=True).first()
            if intent is None:
                break
            lock_object_path(session, intent.object_name)
            intent.attempts += 1
            try:
                referenced = session.query(File.id).filter(File.file_path == intent.object_name).first()
                if referenced is None:
                    storage_factory().delete_file(intent.object_name)
            except Exception as error:
                intent.last_error_type = type(error).__name__[:96]
                intent.next_attempt_at = now + timedelta(seconds=min(300, 2 ** min(intent.attempts, 9)))
                session.commit()
                logger.warning("Object cleanup {} deferred ({})", intent.id, intent.last_error_type)
            else:
                intent.completed_at = utc_now()
                intent.last_error_type = None
                session.commit()
                completed += 1
    return completed

