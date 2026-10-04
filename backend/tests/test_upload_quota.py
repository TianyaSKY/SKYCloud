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


def test_object_cleanup_is_retryable_and_preserves_referenced_files(session, test_workspace, test_user, tmp_path, monkeypatch, mock_object_storage):
    uid, wid = test_user.id, test_workspace.id
    mock_object_storage.objects['orphan-object'] = b'orphan'
    row = quota.reserve(session, wid, uid, 6, details={'object_name': 'orphan-object'})
    rid = row.id
    quota.cancel(session, wid, uid, rid)
    broken = MagicMock()
    broken.delete_file.side_effect = RuntimeError('storage unavailable')
    quota.cleanup(session, str(tmp_path), storage_factory=lambda: broken)
    assert session.get(UploadReservation, rid).cleaned_at is None
    assert mock_object_storage.file_exists('orphan-object')
    quota.cleanup(session, str(tmp_path), storage_factory=lambda: mock_object_storage)
    assert not mock_object_storage.file_exists('orphan-object')
    assert session.get(UploadReservation, rid).cleaned_at is not None
    mock_object_storage.objects['referenced-object'] = b'keep'
    rid = quota.reserve(session, wid, uid, 4, details={'object_name': 'referenced-object'}).id
    quota.consume(session, wid, uid, rid, 4)
    session.add(File(name='keep', file_path='referenced-object', file_size=4, workspace_id=wid, uploader_id=uid))
    session.commit()
    quota.cleanup(session, str(tmp_path), storage_factory=lambda: mock_object_storage)
    assert mock_object_storage.file_exists('referenced-object')


def test_expired_reservation_of_live_object_writer_is_not_cleaned(session, test_workspace, test_user, tmp_path, mock_object_storage):
    from app.infra.upload_staging import StagedUpload
    uid, wid = test_user.id, test_workspace.id
    stage = StagedUpload(tmp_path)
    rid = quota.reserve(session, wid, uid, 5, details={'object_name': 'in-flight', 'stage_id': stage.directory.name}).id
    row = session.get(UploadReservation, rid)
    row.expires_at = quota.utc_now() - timedelta(seconds=1)
    session.commit()
    quota.cleanup(session, str(tmp_path / 'parts'), upload_root=str(tmp_path), storage_factory=lambda: mock_object_storage)
    assert session.get(UploadReservation, rid).cleaned_at is None
    mock_object_storage.objects['in-flight'] = b'orphan'
    stage.handle.close()  # Emulate process death after remote upload.
    quota.cleanup(session, str(tmp_path / 'parts'), upload_root=str(tmp_path), storage_factory=lambda: mock_object_storage)
    assert not mock_object_storage.file_exists('in-flight')
    assert session.get(UploadReservation, rid).cleaned_at is not None


def test_uncertain_objects_keep_capacity_reserved_until_cleanup_succeeds(session, test_workspace, test_user, tmp_path, monkeypatch, mock_object_storage):
    uid, wid = test_user.id, test_workspace.id
    monkeypatch.setattr(quota, 'USER_CAPACITY', 10)
    rid = quota.reserve(session, wid, uid, 10, details={'object_name': 'uncertain'}).id
    quota.cancel(session, wid, uid, rid)
    with pytest.raises(BusinessRuleError, match='capacity'):
        quota.reserve(session, wid, uid, 1)
    quota.cleanup(session, str(tmp_path), storage_factory=lambda: mock_object_storage)
    assert quota.reserve(session, wid, uid, 10).size == 10


def test_lost_upload_success_response_still_deletes_object(session, test_workspace, test_user, tmp_path, monkeypatch, mock_object_storage):
    from app.exceptions import ServiceOperationError
    uid, wid = test_user.id, test_workspace.id
    monkeypatch.setattr(service, 'UPLOAD_FOLDER', str(tmp_path))
    original_upload = mock_object_storage.upload_file
    def lost_response(path, name):
        original_upload(path, name)
        assert mock_object_storage.file_exists(name)
        raise RuntimeError('PUT response lost')
    monkeypatch.setattr(mock_object_storage, 'upload_file', lost_response)
    with pytest.raises(ServiceOperationError):
        service.create_file(session, adapter(b'12345'), {'workspace_id': wid, 'uploader_id': uid})
    assert mock_object_storage.objects == {}
    assert session.query(File).count() == 0
    assert list(tmp_path.iterdir()) == []


def test_legacy_upload_session_returns_reinitialization_error(session, test_workspace, test_user, tmp_path, monkeypatch):
    uid, wid = test_user.id, test_workspace.id
    monkeypatch.setattr(service, 'MULTIPART_ROOT', str(tmp_path))
    rid = service.init_multipart_upload(session, wid, uid, {'filename': 'legacy', 'total_size': 5})['upload_id']
    row = session.get(UploadReservation, rid)
    row.details = {key: value for key, value in row.details.items() if key not in {'object_name', 'stage_id'}}
    session.commit()
    with pytest.raises(ConflictError, match='reinitialized'):
        service.complete_multipart_upload(session, wid, uid, rid)


def test_workspace_concurrency_applies_across_users(session, test_workspace, test_user, monkeypatch):
    uid, wid = test_user.id, test_workspace.id
    monkeypatch.setattr(quota, 'WORKSPACE_CONCURRENCY', 1)
    other = User(username='concurrent-member', password_hash='x')
    session.add(other)
    session.commit()
    quota.reserve(session, wid, uid, 1)
    with pytest.raises(ConflictError, match='active'):
        quota.reserve(session, wid, other.id, 1)
