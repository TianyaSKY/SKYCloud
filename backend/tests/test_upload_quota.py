from datetime import timedelta
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import UploadFile
from app.exceptions import BusinessRuleError, ConflictError, PermissionDeniedError
from app.features.file import service, upload_quota as quota
from app.infra.upload_adapter import FastAPIUploadAdapter
from app.models.file import File
from app.models.upload_reservation import UploadDailyUsage, UploadReservation
from app.models.user import User


def adapter(data):
    return FastAPIUploadAdapter(UploadFile(filename='x', file=BytesIO(data)))


def test_capacity_includes_pending_and_stored_files(session, test_workspace, test_user, monkeypatch):
    uid, wid = test_user.id, test_workspace.id
    monkeypatch.setattr(quota, 'USER_CAPACITY', 10)
    reservation = quota.reserve(session, wid, uid, 6)
    with pytest.raises(BusinessRuleError, match='capacity'):
        quota.reserve(session, wid, uid, 5)
    quota.cancel(session, wid, uid, reservation.id)
    record = File(name='x', file_path='x', file_size=7, workspace_id=wid, uploader_id=uid)
    session.add(record)
    session.commit()
    with pytest.raises(BusinessRuleError, match='capacity'):
        quota.reserve(session, wid, uid, 4)


def test_workspace_capacity_shared_between_members(session, test_workspace, test_user, monkeypatch):
    monkeypatch.setattr(quota, 'WORKSPACE_CAPACITY', 10)
    uid, wid = test_user.id, test_workspace.id
    other = User(username='other-quota', password_hash='x')
    session.add(other)
    session.commit()
    quota.reserve(session, wid, uid, 6)
    with pytest.raises(BusinessRuleError, match='capacity'):
        quota.reserve(session, wid, other.id, 5)


def test_abort_does_not_refund_daily_admission(session, test_workspace, test_user, monkeypatch):
    monkeypatch.setattr(quota, 'DAILY_ATTEMPTS', 1)
    uid, wid = test_user.id, test_workspace.id
    reservation_id = quota.reserve(session, wid, uid, 5).id
    quota.cancel(session, wid, uid, reservation_id)
    with pytest.raises(BusinessRuleError, match='Daily'):
        quota.reserve(session, wid, uid, 1)
    usage = session.get(UploadDailyUsage, (uid, quota.utc_now().date()))
    assert (usage.attempts, usage.bytes) == (1, 5)


def test_daily_bytes_and_concurrency(session, test_workspace, test_user, monkeypatch):
    uid, wid = test_user.id, test_workspace.id
    monkeypatch.setattr(quota, 'DAILY_BYTES', 10)
    monkeypatch.setattr(quota, 'USER_CONCURRENCY', 1)
    reservation_id = quota.reserve(session, wid, uid, 7).id
    with pytest.raises(ConflictError, match='active'):
        quota.reserve(session, wid, uid, 1)
    quota.cancel(session, wid, uid, reservation_id)
    with pytest.raises(BusinessRuleError, match='Daily'):
        quota.reserve(session, wid, uid, 4)


def test_expiration_reclaims_capacity_and_disk(session, test_workspace, test_user, tmp_path, monkeypatch):
    uid, wid = test_user.id, test_workspace.id
    monkeypatch.setattr(quota, 'USER_CAPACITY', 10)
    row = quota.reserve(session, wid, uid, 10, kind='multipart')
    rid = row.id
    row.expires_at = quota.utc_now() - timedelta(seconds=1)
    session.commit()
    directory = tmp_path / str(wid) / rid
    directory.mkdir(parents=True)
    (directory / 'part').write_bytes(b'x')
    with pytest.raises(ConflictError, match='expired'):
        quota.owned(session, wid, uid, rid)
    assert quota.cleanup(session, str(tmp_path)) == 1
    assert not directory.exists()
    assert session.get(UploadReservation, rid).status == 'expired'
    assert quota.reserve(session, wid, uid, 10).size == 10


def test_multipart_ownership_resume_and_once_only_completion(session, test_workspace, test_user, tmp_path, monkeypatch):
    uid, wid = test_user.id, test_workspace.id
    monkeypatch.setattr(service, 'MULTIPART_ROOT', str(tmp_path / 'parts'))
    monkeypatch.setattr(service, 'UPLOAD_FOLDER', str(tmp_path / 'uploads'))
    monkeypatch.setattr(service, '_push_processing_queue', MagicMock())
    other = User(username='other-upload', password_hash='x')
    session.add(other)
    session.commit()
    oid = other.id
    data = {'filename': 'x', 'total_size': 5, 'chunk_size': 5}
    result = service.init_multipart_upload(session, wid, uid, data)
    rid = result['upload_id']
    usage = session.get(UploadDailyUsage, (uid, quota.utc_now().date()))
    assert usage.attempts == 1
    assert service.init_multipart_upload(session, wid, uid, {**data, 'upload_id': rid})['upload_id'] == rid
    assert session.get(UploadDailyUsage, (uid, quota.utc_now().date())).attempts == 1
    for operation in [
        lambda: service.init_multipart_upload(session, wid, oid, {**data, 'upload_id': rid}),
        lambda: service.get_multipart_upload_status(session, wid, oid, rid),
        lambda: service.save_multipart_chunk(session, oid, wid, rid, 0, adapter(b'12345')),
        lambda: service.complete_multipart_upload(session, wid, oid, rid),
        lambda: service.abort_multipart_upload(session, wid, oid, rid),
    ]:
        with pytest.raises(PermissionDeniedError):
            operation()
    service.save_multipart_chunk(session, uid, wid, rid, 0, adapter(b'12345'))
    file = service.complete_multipart_upload(session, wid, uid, rid)
    assert file.file_size == 5
    assert session.get(UploadReservation, rid).status == 'completed'
    with pytest.raises(ConflictError):
        service.complete_multipart_upload(session, wid, uid, rid)
    assert session.query(File).filter_by(workspace_id=wid).count() == 1


def test_instant_upload_copies_are_charged_to_capacity(session, test_workspace, test_user, tmp_path, monkeypatch, mock_object_storage):
    uid, wid = test_user.id, test_workspace.id
    monkeypatch.setattr(service, 'UPLOAD_FOLDER', str(tmp_path))
    monkeypatch.setattr(service, '_push_processing_queue', MagicMock())
    monkeypatch.setattr(quota, 'USER_CAPACITY', 10)
    file = service.create_file(session, adapter(b'12345'), {'workspace_id': wid, 'uploader_id': uid})
    service.preflight_file_upload(session, wid, uid, {'filename': 'copy', 'total_size': 5, 'content_hash': file.content_hash})
    assert session.query(File).count() == 2
    with pytest.raises(BusinessRuleError, match='capacity'):
        service.preflight_file_upload(session, wid, uid, {'filename': 'copy2', 'total_size': 5, 'content_hash': file.content_hash})
    assert session.query(File).count() == 2


def test_hash_mismatch_releases_reservation_without_stored_object(session, test_workspace, test_user, tmp_path, monkeypatch, mock_object_storage):
    uid, wid = test_user.id, test_workspace.id
    monkeypatch.setattr(service, 'MULTIPART_ROOT', str(tmp_path / 'parts'))
    monkeypatch.setattr(service, 'UPLOAD_FOLDER', str(tmp_path / 'uploads'))
    rid = service.init_multipart_upload(session, wid, uid, {'filename': 'x', 'total_size': 5, 'content_hash': 'a' * 64})['upload_id']
    service.save_multipart_chunk(session, uid, wid, rid, 0, adapter(b'12345'))
    with pytest.raises(BusinessRuleError, match='hash'):
        service.complete_multipart_upload(session, wid, uid, rid)
    assert session.get(UploadReservation, rid).status == 'cancelled'
    assert mock_object_storage.objects == {}
    assert not (tmp_path / 'parts' / str(wid) / rid).exists()


def test_batch_capacity_failure_cleans_objects_and_releases_all_reservations(session, test_workspace, test_user, tmp_path, monkeypatch, mock_object_storage):
    uid, wid = test_user.id, test_workspace.id
    monkeypatch.setattr(service, 'UPLOAD_FOLDER', str(tmp_path))
    monkeypatch.setattr(quota, 'USER_CAPACITY', 10)
    with pytest.raises(BusinessRuleError, match='capacity'):
        service.batch_create_files(session, [adapter(b'12345'), adapter(b'123456')], {'workspace_id': wid, 'uploader_id': uid})
    assert mock_object_storage.objects == {}
    assert session.query(File).count() == 0
    assert session.query(UploadReservation).filter_by(status='active').count() == 0
    assert list(tmp_path.iterdir()) == []
    assert quota.reserve(session, wid, uid, 10).size == 10


def test_daily_allowance_rolls_at_utc_midnight(session, test_workspace, test_user, monkeypatch):
    from datetime import datetime
    uid, wid = test_user.id, test_workspace.id
    monkeypatch.setattr(quota, 'DAILY_ATTEMPTS', 1)
    monkeypatch.setattr(quota, 'utc_now', lambda: datetime(2026, 1, 1, 23, 59))
    rid = quota.reserve(session, wid, uid, 5).id
    quota.cancel(session, wid, uid, rid)
    monkeypatch.setattr(quota, 'utc_now', lambda: datetime(2026, 1, 2, 0, 1))
    quota.reserve(session, wid, uid, 5)
    assert session.query(UploadDailyUsage).count() == 2
    assert sum(row.attempts for row in session.query(UploadDailyUsage).all()) == 2
