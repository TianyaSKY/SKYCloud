"""Current edit permission at upload admission and SQL publication boundaries."""

from sqlalchemy.orm import Session

from app.features.file import upload_quota
from app.features.file.upload_quota import ReserveParams
from app.features.workspace.permissions import assert_can_write_locked
from app.models.upload_reservation import UploadReservation


def reserve(session: Session, params: ReserveParams) -> UploadReservation:
    """Check current write access, then admit the reservation."""
    assert_can_write_locked(session, params.workspace_id, params.user_id)
    return upload_quota.reserve(session, params)


def consume(
    session: Session,
    workspace_id: int,
    user_id: int,
    reservation_id: str,
    size: int,
) -> UploadReservation:
    """Check current write access, then mark the reservation completed."""
    # The caller publishes File/outbox in this transaction, without an
    # intervening commit. A role mutation must wait for that outcome.
    assert_can_write_locked(session, workspace_id, user_id)
    return upload_quota.consume(session, workspace_id, user_id, reservation_id, size)
