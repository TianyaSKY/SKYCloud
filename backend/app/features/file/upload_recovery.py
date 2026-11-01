"""Best-effort upload compensation; durable reservations retain unfinished cleanup."""

from collections.abc import Iterable
from dataclasses import dataclass

from loguru import logger
from sqlalchemy.orm import Session

from app.features.file import repository, upload_quota
from app.features.file.upload_quota import CancelParams
from app.features.file.upload_types import UploadStorage


@dataclass(frozen=True, slots=True, kw_only=True)
class ReleaseFailedUploadParams:
    """Identity of one failed upload whose reservation needs cleanup."""

    workspace_id: int
    uploader_id: int
    reservation_id: str | None
    object_name: str


def _release_failed_upload(
    session: Session,
    params: ReleaseFailedUploadParams,
    storage: UploadStorage | None,
) -> None:
    """Release one reservation and delete its object when nothing references it."""
    if params.reservation_id is None:
        return
    # COMMIT may have succeeded even when its acknowledgement failed. Refresh
    # durable reservation state before compensating any remote side effect.
    reservation = upload_quota.lock_for_cancellation(
        session,
        params.workspace_id,
        params.uploader_id,
        params.reservation_id,
        missing_ok=True,
    )
    if reservation is None:
        return
    if reservation.status == "completed":
        session.rollback()
        return
    referenced = repository.has_object_reference(session, params.object_name)
    deleted = False
    if storage is not None and not referenced:
        try:
            # A failed PUT response may still have created the object. Delete the
            # reserved unique key even when upload_file never reported success.
            storage.delete_file(params.object_name)
            deleted = True
        except Exception:
            logger.warning(
                "Object deletion deferred for upload {}", params.reservation_id,
            )
    upload_quota.cancel(
        session,
        CancelParams(
            workspace_id=params.workspace_id,
            user_id=params.uploader_id,
            reservation_id=params.reservation_id,
            missing_ok=True,
            object_deleted=deleted,
        ),
    )


def _reset_failed_transaction(session: Session) -> bool:
    """Discard the failed transaction, falling back to connection invalidation."""
    try:
        session.rollback()
    except Exception as exc:
        logger.warning("Upload rollback failed: error_type={}", type(exc).__name__)
    else:
        return True
    try:
        # SQLAlchemy invalidation discards the failed connection; the same
        # Session can then start a fresh transaction for journal reconciliation.
        session.invalidate()
    except Exception as exc:
        logger.warning(
            "Upload connection invalidation failed: error_type={}", type(exc).__name__,
        )
        return False
    return True


def compensate_failed_uploads(
    session: Session,
    workspace_id: int,
    uploader_id: int,
    uploads: Iterable[tuple[str | None, str]],
    storage: UploadStorage | None,
) -> None:
    """Preserve the caller's error and continue independent reservation cleanup."""
    if not _reset_failed_transaction(session):
        return
    for reservation_id, object_name in uploads:
        params = ReleaseFailedUploadParams(
            workspace_id=workspace_id,
            uploader_id=uploader_id,
            reservation_id=reservation_id,
            object_name=object_name,
        )
        try:
            _release_failed_upload(session, params, storage)
        except Exception as exc:
            logger.warning(
                "Upload compensation deferred: error_type={}", type(exc).__name__,
            )
            # The admission journal was committed before object IO. Keep failed
            # entries for the expiration worker, and try the remaining entries.
            if not _reset_failed_transaction(session):
                break
