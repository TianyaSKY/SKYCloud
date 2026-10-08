"""Best-effort upload compensation; durable reservations retain unfinished cleanup."""
from loguru import logger

from app.features.file import repository, upload_quota


def _release_failed_upload(session, workspace_id, uploader_id, reservation_id, object_name, storage):
    if reservation_id is None:
        return
    # COMMIT may have succeeded even when its acknowledgement failed. Refresh
    # durable reservation state before compensating any remote side effect.
    reservation = upload_quota.lock_for_cancellation(
        session, workspace_id, uploader_id, reservation_id, missing_ok=True)
    if reservation is None:
        return
    if reservation.status == "completed":
        session.rollback()
        return
    referenced = repository.has_object_reference(session, object_name)
    deleted = False
    if storage is not None and not referenced:
        try:
            # A failed PUT response may still have created the object. Delete the
            # reserved unique key even when upload_file never reported success.
            storage.delete_file(object_name)
            deleted = True
        except Exception:
            logger.warning("Object deletion deferred for upload {}", reservation_id)
    upload_quota.cancel(session, workspace_id, uploader_id, reservation_id,
                        missing_ok=True, object_deleted=deleted)


def _reset_failed_transaction(session):
    try:
        session.rollback()
        return True
    except Exception as exc:
        logger.warning("Upload rollback failed: error_type={}", type(exc).__name__)
    try:
        # SQLAlchemy invalidation discards the failed connection; the same
        # Session can then start a fresh transaction for journal reconciliation.
        session.invalidate()
        return True
    except Exception as exc:
        logger.warning("Upload connection invalidation failed: error_type={}", type(exc).__name__)
        return False


def compensate_failed_uploads(session, workspace_id, uploader_id, uploads, storage):
    """Preserve the caller's error and continue independent reservation cleanup."""
    if not _reset_failed_transaction(session):
        return
    for reservation_id, object_name in uploads:
        try:
            _release_failed_upload(session, workspace_id, uploader_id, reservation_id, object_name, storage)
        except Exception as exc:
            logger.warning("Upload compensation deferred: error_type={}", type(exc).__name__)
            # The admission journal was committed before object IO. Keep failed
            # entries for the expiration worker, and try the remaining entries.
            if not _reset_failed_transaction(session):
                break
