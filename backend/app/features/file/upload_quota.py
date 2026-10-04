"""Quota admission is serialized by existing user/workspace rows, not process locks.

Reservations commit before object IO; consuming a reservation and inserting its
file share a transaction. Daily admission charges survive aborts and IO failures.
All deadlines and day boundaries are UTC, stored as UTC naive timestamps.
"""
from datetime import datetime, timedelta, timezone
import uuid

from sqlalchemy import delete, func, or_, select
from app.exceptions import BusinessRuleError, ConflictError, PermissionDeniedError, ResourceNotFoundError
from app.infra.upload_limits import positive_limit
from app.models.file import File
from app.models.user import User
from app.models.workspace import Workspace
from app.models.upload_reservation import UploadDailyUsage, UploadReservation

USER_CAPACITY = positive_limit('UPLOAD_USER_CAPACITY', 10 * 1024**3)
WORKSPACE_CAPACITY = positive_limit('UPLOAD_WORKSPACE_CAPACITY', 50 * 1024**3)
DAILY_BYTES = positive_limit('UPLOAD_DAILY_BYTES', 5 * 1024**3)
DAILY_ATTEMPTS = positive_limit('UPLOAD_DAILY_ATTEMPTS', 200)
USER_CONCURRENCY = positive_limit('UPLOAD_USER_CONCURRENCY', 8)
WORKSPACE_CONCURRENCY = positive_limit('UPLOAD_WORKSPACE_CONCURRENCY', 32)
TTL_SECONDS = positive_limit('UPLOAD_RESERVATION_TTL_SECONDS', 3600)


def utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def lock_owners(session, workspace_id, user_id):
    # Every admission/completion uses this order, including cross-workspace uploads.
    if session.scalar(select(User.id).where(User.id == user_id).with_for_update()) is None:
        raise ResourceNotFoundError('Upload owner not found')
    if session.scalar(select(Workspace.id).where(Workspace.id == workspace_id).with_for_update()) is None:
        raise ResourceNotFoundError('Workspace not found')


def owned(session, workspace_id, user_id, reservation_id, *, lock=True):
    if lock:
        lock_owners(session, workspace_id, user_id)
    query = select(UploadReservation).where(UploadReservation.id == reservation_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    row = session.scalar(query)
    if row is None:
        raise ResourceNotFoundError('Upload session not found')
    if row.workspace_id != workspace_id or row.user_id != user_id:
        raise PermissionDeniedError('Upload session belongs to another user or workspace')
    if row.status != 'active' or row.expires_at <= utc_now():
        raise ConflictError('Upload session is closed or expired')
    return row


def reserve(session, workspace_id, user_id, size, *, kind='file', reservation_id=None, details=None):
    if size < 0:
        raise BusinessRuleError('Invalid upload size')
    lock_owners(session, workspace_id, user_id)
    reservation_id = reservation_id or uuid.uuid4().hex
    if session.get(UploadReservation, reservation_id) is not None:
        raise ConflictError('Upload session already exists')
    now = utc_now()
    active = [UploadReservation.status == 'active', UploadReservation.expires_at > now]
    for column, owner, capacity, concurrency, file_column in [
        (UploadReservation.user_id, user_id, USER_CAPACITY, USER_CONCURRENCY, File.uploader_id),
        (UploadReservation.workspace_id, workspace_id, WORKSPACE_CAPACITY, WORKSPACE_CONCURRENCY, File.workspace_id),
    ]:
        stored = session.scalar(select(func.coalesce(func.sum(File.file_size), 0)).where(file_column == owner))
        pending, count = session.execute(select(func.coalesce(func.sum(UploadReservation.size), 0), func.count())
                                         .where(column == owner, *active)).one()
        if stored + pending + size > capacity:
            raise BusinessRuleError('Upload storage capacity exceeded')
        if count >= concurrency:
            raise ConflictError('Too many active uploads')
    usage = session.get(UploadDailyUsage, (user_id, now.date()), populate_existing=True)
    if usage is None:
        usage = UploadDailyUsage(user_id=user_id, day=now.date(), attempts=0, bytes=0)
        session.add(usage)
    if usage.attempts >= DAILY_ATTEMPTS or usage.bytes + size > DAILY_BYTES:
        raise BusinessRuleError('Daily upload allowance exceeded')
    usage.attempts += 1
    usage.bytes += size
    row = UploadReservation(id=reservation_id, user_id=user_id, workspace_id=workspace_id,
                            size=size, kind=kind, status='active', created_at=now,
                            expires_at=now + timedelta(seconds=TTL_SECONDS), details=details or {})
    session.add(row)
    session.commit()
    return row


def consume(session, workspace_id, user_id, reservation_id, size):
    row = owned(session, workspace_id, user_id, reservation_id)
    if size != row.size:
        raise BusinessRuleError('Upload size differs from reservation')
    row.status = 'completed'
    # Caller commits this with File; on rollback it stays active.
    return row


def cancel(session, workspace_id, user_id, reservation_id, *, missing_ok=False):
    if missing_ok and session.get(UploadReservation, reservation_id) is None:
        session.rollback()
        return
    lock_owners(session, workspace_id, user_id)
    row = session.scalar(select(UploadReservation).where(UploadReservation.id == reservation_id)
                         .with_for_update().execution_options(populate_existing=True))
    if row is None or row.user_id != user_id or row.workspace_id != workspace_id:
        raise PermissionDeniedError('Upload session belongs to another user or workspace')
    if row.status == 'active':
        row.status = 'cancelled'
    session.commit()


def cleanup(session, multipart_root):
    """Reclaim expired/closed sessions under the same locks as writers."""
    import os
    import re
    import shutil
    now = utc_now()
    ids = session.execute(select(UploadReservation.id, UploadReservation.workspace_id, UploadReservation.user_id)
        .where(UploadReservation.cleaned_at.is_(None),
               or_(UploadReservation.status != 'active', UploadReservation.expires_at <= now))
        .order_by(UploadReservation.expires_at).limit(1000)).all()
    removed = 0
    for reservation_id, workspace_id, user_id in ids:
        lock_owners(session, workspace_id, user_id)
        row = session.scalar(select(UploadReservation).where(UploadReservation.id == reservation_id)
                             .with_for_update().execution_options(populate_existing=True))
        if row is None or row.cleaned_at is not None or (row.status == 'active' and row.expires_at > now):
            session.rollback()
            continue
        if row.status == 'active':
            row.status = 'expired'
        # Database identity cannot supply path components outside the upload root.
        if workspace_id > 0 and re.fullmatch(r'[A-Za-z0-9_-]{8,128}', reservation_id):
            path = os.path.join(multipart_root, str(workspace_id), reservation_id)
            if os.path.isdir(path):
                shutil.rmtree(path)
                removed += 1
        row.cleaned_at = now
        session.commit()
    # Retain recent closed-session responses; admission charges outlive the session.
    session.execute(delete(UploadReservation).where(UploadReservation.cleaned_at.is_not(None),
                    UploadReservation.expires_at < now - timedelta(days=7)))
    session.execute(delete(UploadDailyUsage).where(UploadDailyUsage.day < (now - timedelta(days=90)).date()))
    session.commit()
    return removed
