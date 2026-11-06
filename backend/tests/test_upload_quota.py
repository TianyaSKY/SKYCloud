from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from fastapi import UploadFile
from sqlalchemy import delete, update
from sqlalchemy.orm import Session

from app.exceptions import (
    BusinessRuleError,
    ConflictError,
    PermissionDeniedError,
    ServiceOperationError,
)
from app.features.auth import account_repository
from app.features.file import (
    avatar_service,
    publication,
    upload_config,
    upload_quota as quota,
)
from app.features.file.multipart_service import (
    MultipartInitData,
    SaveMultipartChunkParams,
    get_multipart_service,
)
from app.features.file.upload_config import (
    multipart_settings,
    upload_settings,
)
from app.features.file.upload_service import get_upload_service
from app.features.share import service as shares
from app.infra.upload_adapter import FastAPIUploadAdapter
from app.infra.upload_staging import StagedUpload
from app.models.file import File
from app.models.file_task_outbox import FileTaskOutbox
from app.models.share import Share
from app.models.upload_reservation import (
    UploadDailyUsage,
    UploadReservation,
)
from app.models.user import User
from app.models.workspace import Workspace
from tests.conftest import InMemoryStorageClient


def adapter(data: bytes) -> FastAPIUploadAdapter:
    return FastAPIUploadAdapter(UploadFile(filename="x", file=BytesIO(data)))


def test_capacity_includes_pending_and_stored_files(
    session: Session,
    test_workspace: Workspace,
    test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid, wid = int(test_user.id), int(test_workspace.id)
    monkeypatch.setattr(quota, "USER_CAPACITY", 10)
    reservation = quota.reserve(
        session, quota.ReserveParams(workspace_id=wid, user_id=uid, size=6)
    )
    with pytest.raises(BusinessRuleError, match="capacity"):
        quota.reserve(
            session, quota.ReserveParams(workspace_id=wid, user_id=uid, size=5)
        )
    quota.cancel(
        session,
        quota.CancelParams(
            workspace_id=wid, user_id=uid, reservation_id=reservation.id
        ),
    )
    record = File(
        name="x", file_path="x", file_size=7, workspace_id=wid, uploader_id=uid
    )
    session.add(record)
    session.commit()
    with pytest.raises(BusinessRuleError, match="capacity"):
        quota.reserve(
            session, quota.ReserveParams(workspace_id=wid, user_id=uid, size=4)
        )


def test_workspace_capacity_shared_between_members(
    session: Session,
    test_workspace: Workspace,
    test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(quota, "WORKSPACE_CAPACITY", 10)
    uid, wid = int(test_user.id), int(test_workspace.id)
    other = User(username="other-quota", password_hash="x")
    session.add(other)
    session.commit()
    quota.reserve(
        session, quota.ReserveParams(workspace_id=wid, user_id=uid, size=6)
    )
    with pytest.raises(BusinessRuleError, match="capacity"):
        quota.reserve(
            session,
            quota.ReserveParams(workspace_id=wid, user_id=int(other.id), size=5),
        )


def test_abort_does_not_refund_daily_admission(
    session: Session,
    test_workspace: Workspace,
    test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(quota, "DAILY_ATTEMPTS", 1)
    uid, wid = int(test_user.id), int(test_workspace.id)
    reservation_id = quota.reserve(
        session, quota.ReserveParams(workspace_id=wid, user_id=uid, size=5)
    ).id
    quota.cancel(
        session,
        quota.CancelParams(
            workspace_id=wid, user_id=uid, reservation_id=reservation_id
        ),
    )
    with pytest.raises(BusinessRuleError, match="Daily"):
        quota.reserve(
            session, quota.ReserveParams(workspace_id=wid, user_id=uid, size=1)
        )
    usage = session.get(UploadDailyUsage, (uid, quota.utc_now().date()))
    assert usage is not None
    assert (usage.attempts, usage.bytes) == (1, 5)


def test_daily_bytes_and_concurrency(
    session: Session,
    test_workspace: Workspace,
    test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid, wid = int(test_user.id), int(test_workspace.id)
    monkeypatch.setattr(quota, "DAILY_BYTES", 10)
    monkeypatch.setattr(quota, "USER_CONCURRENCY", 1)
    reservation_id = quota.reserve(
        session, quota.ReserveParams(workspace_id=wid, user_id=uid, size=7)
    ).id
    with pytest.raises(ConflictError, match="active"):
        quota.reserve(
            session, quota.ReserveParams(workspace_id=wid, user_id=uid, size=1)
        )
    quota.cancel(
        session,
        quota.CancelParams(
            workspace_id=wid, user_id=uid, reservation_id=reservation_id
        ),
    )
    with pytest.raises(BusinessRuleError, match="Daily"):
        quota.reserve(
            session, quota.ReserveParams(workspace_id=wid, user_id=uid, size=4)
        )


def test_expiration_reclaims_capacity_and_disk(
    session: Session,
    test_workspace: Workspace,
    test_user: User,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid, wid = int(test_user.id), int(test_workspace.id)
    monkeypatch.setattr(quota, "USER_CAPACITY", 10)
    row = quota.reserve(
        session,
        quota.ReserveParams(
            workspace_id=wid, user_id=uid, size=10, kind="multipart"
        ),
    )
    rid = row.id
    row.expires_at = quota.utc_now() - timedelta(seconds=1)
    session.commit()
    directory = tmp_path / str(wid) / rid
    directory.mkdir(parents=True)
    (directory / "part").write_bytes(b"x")
    with pytest.raises(ConflictError, match="expired"):
        quota.owned(session, wid, uid, rid)
    assert quota.cleanup(session, str(tmp_path)) == 1
    assert not directory.exists()
    reservation = session.get(UploadReservation, rid)
    assert reservation is not None and reservation.status == "expired"
    assert (
        quota.reserve(
            session, quota.ReserveParams(workspace_id=wid, user_id=uid, size=10)
        ).size
        == 10
    )


def test_multipart_ownership_resume_and_once_only_completion(
    session: Session,
    test_workspace: Workspace,
    test_user: User,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid, wid = int(test_user.id), int(test_workspace.id)
    monkeypatch.setattr(
        upload_config, "UPLOAD_FOLDER", str(tmp_path / "uploads")
    )
    service = get_multipart_service()
    other = User(username="other-upload", password_hash="x")
    session.add(other)
    session.commit()
    oid = int(other.id)
    data: dict[str, Any] = {"filename": "x", "total_size": 5, "chunk_size": 5}
    result = service.init_multipart_upload(
        session, wid, uid, cast(MultipartInitData, data)
    )
    rid = str(result["upload_id"])
    usage = session.get(UploadDailyUsage, (uid, quota.utc_now().date()))
    assert usage is not None and usage.attempts == 1
    assert (
        service.init_multipart_upload(
            session, wid, uid, cast(MultipartInitData, {**data, "upload_id": rid})
        )["upload_id"]
        == rid
    )
    current_usage = session.get(UploadDailyUsage, (uid, quota.utc_now().date()))
    assert current_usage is not None and current_usage.attempts == 1
    for operation in [
        lambda: service.init_multipart_upload(
            session,
            wid,
            oid,
            cast(MultipartInitData, {**data, "upload_id": rid}),
        ),
        lambda: service.get_multipart_upload_status(session, wid, oid, rid),
        lambda: service.save_multipart_chunk(
            session,
            SaveMultipartChunkParams(
                uploader_id=oid,
                workspace_id=wid,
                upload_id=rid,
                chunk_index=0,
                chunk=adapter(b"12345"),
            ),
        ),
        lambda: service.complete_multipart_upload(session, wid, oid, rid),
        lambda: service.abort_multipart_upload(session, wid, oid, rid),
    ]:
        with pytest.raises(PermissionDeniedError):
            operation()
    service.save_multipart_chunk(
        session,
        SaveMultipartChunkParams(
            uploader_id=uid,
            workspace_id=wid,
            upload_id=rid,
            chunk_index=0,
            chunk=adapter(b"12345"),
        ),
    )
    file = service.complete_multipart_upload(session, wid, uid, rid)
    assert file.file_size == 5
    reservation = session.get(UploadReservation, rid)
    assert reservation is not None and reservation.status == "completed"
    with pytest.raises(ConflictError):
        service.complete_multipart_upload(session, wid, uid, rid)
    assert session.query(File).filter_by(workspace_id=wid).count() == 1


def test_instant_upload_copies_are_charged_to_capacity(
    session: Session,
    test_workspace: Workspace,
    test_user: User,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_object_storage: InMemoryStorageClient,
) -> None:
    uid, wid = int(test_user.id), int(test_workspace.id)
    monkeypatch.setattr(upload_config, "UPLOAD_FOLDER", str(tmp_path))
    monkeypatch.setattr(quota, "USER_CAPACITY", 10)
    service = get_upload_service()
    file = service.create_uploaded_file(session, wid, uid, adapter(b"12345"))
    service.preflight_file_upload(
        session,
        wid,
        uid,
        {
            "filename": "copy",
            "total_size": 5,
            "content_hash": file.content_hash,
        },
    )
    assert session.query(File).count() == 2
    with pytest.raises(BusinessRuleError, match="capacity"):
        service.preflight_file_upload(
            session,
            wid,
            uid,
            {
                "filename": "copy2",
                "total_size": 5,
                "content_hash": file.content_hash,
            },
        )
    assert session.query(File).count() == 2


def test_hash_mismatch_releases_reservation_without_stored_object(
    session: Session,
    test_workspace: Workspace,
    test_user: User,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_object_storage: InMemoryStorageClient,
) -> None:
    uid, wid = int(test_user.id), int(test_workspace.id)
    upload_root = tmp_path / "uploads"
    monkeypatch.setattr(upload_config, "UPLOAD_FOLDER", str(upload_root))
    service = get_multipart_service()
    rid = str(
        service.init_multipart_upload(
            session,
            wid,
            uid,
            {"filename": "x", "total_size": 5, "content_hash": "a" * 64},
        )["upload_id"]
    )
    service.save_multipart_chunk(
        session,
        SaveMultipartChunkParams(
            uploader_id=uid,
            workspace_id=wid,
            upload_id=rid,
            chunk_index=0,
            chunk=adapter(b"12345"),
        ),
    )
    with pytest.raises(BusinessRuleError, match="hash"):
        service.complete_multipart_upload(session, wid, uid, rid)
    reservation = session.get(UploadReservation, rid)
    assert reservation is not None and reservation.status == "cancelled"
    assert mock_object_storage.objects == {}
    assert not (upload_root / ".multipart" / str(wid) / rid).exists()


def test_batch_capacity_failure_cleans_objects_and_releases_all_reservations(
    session: Session,
    test_workspace: Workspace,
    test_user: User,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_object_storage: InMemoryStorageClient,
) -> None:
    uid, wid = int(test_user.id), int(test_workspace.id)
    monkeypatch.setattr(upload_config, "UPLOAD_FOLDER", str(tmp_path))
    monkeypatch.setattr(quota, "USER_CAPACITY", 10)
    with pytest.raises(BusinessRuleError, match="capacity"):
        get_upload_service().create_uploaded_files(
            session, wid, uid, [adapter(b"12345"), adapter(b"123456")]
        )
    assert mock_object_storage.objects == {}
    assert session.query(File).count() == 0
    assert (
        session.query(UploadReservation).filter_by(status="active").count() == 0
    )
    assert list(tmp_path.iterdir()) == []
    assert (
        quota.reserve(
            session, quota.ReserveParams(workspace_id=wid, user_id=uid, size=10)
        ).size
        == 10
    )


def test_daily_allowance_rolls_at_utc_midnight(
    session: Session,
    test_workspace: Workspace,
    test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid, wid = int(test_user.id), int(test_workspace.id)
    monkeypatch.setattr(quota, "DAILY_ATTEMPTS", 1)
    monkeypatch.setattr(quota, "utc_now", lambda: datetime(2026, 1, 1, 23, 59))
    rid = quota.reserve(
        session, quota.ReserveParams(workspace_id=wid, user_id=uid, size=5)
    ).id
    quota.cancel(
        session,
        quota.CancelParams(workspace_id=wid, user_id=uid, reservation_id=rid),
    )
    monkeypatch.setattr(quota, "utc_now", lambda: datetime(2026, 1, 2, 0, 1))
    quota.reserve(
        session, quota.ReserveParams(workspace_id=wid, user_id=uid, size=5)
    )
    assert session.query(UploadDailyUsage).count() == 2
    assert (
        sum(int(row.attempts) for row in session.query(UploadDailyUsage).all())
        == 2
    )


def test_object_cleanup_is_retryable_and_preserves_referenced_files(
    session: Session,
    test_workspace: Workspace,
    test_user: User,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_object_storage: InMemoryStorageClient,
) -> None:
    uid, wid = int(test_user.id), int(test_workspace.id)
    mock_object_storage.objects["orphan-object"] = b"orphan"
    row = quota.reserve(
        session,
        quota.ReserveParams(
            workspace_id=wid,
            user_id=uid,
            size=6,
            details={"object_name": "orphan-object"},
        ),
    )
    rid = row.id
    quota.cancel(
        session,
        quota.CancelParams(workspace_id=wid, user_id=uid, reservation_id=rid),
    )
    broken = MagicMock()
    broken.delete_file.side_effect = RuntimeError("storage unavailable")
    quota.cleanup(session, str(tmp_path), storage_factory=lambda: broken)
    row_res = session.get(UploadReservation, rid)
    assert row_res is not None and row_res.cleaned_at is None
    assert mock_object_storage.file_exists("orphan-object")
    quota.cleanup(
        session, str(tmp_path), storage_factory=lambda: mock_object_storage
    )
    assert not mock_object_storage.file_exists("orphan-object")
    row_res2 = session.get(UploadReservation, rid)
    assert row_res2 is not None and row_res2.cleaned_at is not None
    mock_object_storage.objects["referenced-object"] = b"keep"
    rid = quota.reserve(
        session,
        quota.ReserveParams(
            workspace_id=wid,
            user_id=uid,
            size=4,
            details={"object_name": "referenced-object"},
        ),
    ).id
    quota.consume(session, wid, uid, rid, 4)
    session.add(
        File(
            name="keep",
            file_path="referenced-object",
            file_size=4,
            workspace_id=wid,
            uploader_id=uid,
        )
    )
    session.commit()
    quota.cleanup(
        session, str(tmp_path), storage_factory=lambda: mock_object_storage
    )
    assert mock_object_storage.file_exists("referenced-object")


def test_expired_reservation_of_live_object_writer_is_not_cleaned(
    session: Session,
    test_workspace: Workspace,
    test_user: User,
    tmp_path: Path,
    mock_object_storage: InMemoryStorageClient,
) -> None:
    uid, wid = int(test_user.id), int(test_workspace.id)
    stage = StagedUpload(str(tmp_path))
    rid = quota.reserve(
        session,
        quota.ReserveParams(
            workspace_id=wid,
            user_id=uid,
            size=5,
            details={
                "object_name": "in-flight",
                "stage_id": stage.directory.name,
            },
        ),
    ).id
    row = session.get(UploadReservation, rid)
    assert row is not None
    row.expires_at = quota.utc_now() - timedelta(seconds=1)
    session.commit()
    quota.cleanup(
        session,
        str(tmp_path / "parts"),
        upload_root=str(tmp_path),
        storage_factory=lambda: mock_object_storage,
    )
    row_check = session.get(UploadReservation, rid)
    assert row_check is not None and row_check.cleaned_at is None
    mock_object_storage.objects["in-flight"] = b"orphan"
    stage.handle.close()
    quota.cleanup(
        session,
        str(tmp_path / "parts"),
        upload_root=str(tmp_path),
        storage_factory=lambda: mock_object_storage,
    )
    assert not mock_object_storage.file_exists("in-flight")
    row_check2 = session.get(UploadReservation, rid)
    assert row_check2 is not None and row_check2.cleaned_at is not None


def test_uncertain_objects_keep_capacity_reserved_until_cleanup_succeeds(
    session: Session,
    test_workspace: Workspace,
    test_user: User,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_object_storage: InMemoryStorageClient,
) -> None:
    uid, wid = int(test_user.id), int(test_workspace.id)
    monkeypatch.setattr(quota, "USER_CAPACITY", 10)
    rid = quota.reserve(
        session,
        quota.ReserveParams(
            workspace_id=wid,
            user_id=uid,
            size=10,
            details={"object_name": "uncertain"},
        ),
    ).id
    quota.cancel(
        session,
        quota.CancelParams(workspace_id=wid, user_id=uid, reservation_id=rid),
    )
    with pytest.raises(BusinessRuleError, match="capacity"):
        quota.reserve(
            session, quota.ReserveParams(workspace_id=wid, user_id=uid, size=1)
        )
    quota.cleanup(
        session, str(tmp_path), storage_factory=lambda: mock_object_storage
    )
    assert (
        quota.reserve(
            session, quota.ReserveParams(workspace_id=wid, user_id=uid, size=10)
        ).size
        == 10
    )


def test_lost_upload_success_response_still_deletes_object(
    session: Session,
    test_workspace: Workspace,
    test_user: User,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_object_storage: InMemoryStorageClient,
) -> None:
    uid, wid = int(test_user.id), int(test_workspace.id)
    monkeypatch.setattr(upload_config, "UPLOAD_FOLDER", str(tmp_path))
    original_upload = mock_object_storage.upload_file

    def lost_response(path: str, name: str) -> None:
        original_upload(path, name)
        assert mock_object_storage.file_exists(name)
        raise RuntimeError("PUT response lost")

    monkeypatch.setattr(mock_object_storage, "upload_file", lost_response)
    with pytest.raises(ServiceOperationError):
        get_upload_service().create_uploaded_file(
            session, wid, uid, adapter(b"12345")
        )
    assert mock_object_storage.objects == {}
    assert session.query(File).count() == 0
    assert list(tmp_path.iterdir()) == []


def test_legacy_upload_session_returns_reinitialization_error(
    session: Session,
    test_workspace: Workspace,
    test_user: User,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid, wid = int(test_user.id), int(test_workspace.id)
    monkeypatch.setattr(upload_config, "UPLOAD_FOLDER", str(tmp_path))
    service = get_multipart_service()
    rid = str(
        service.init_multipart_upload(
            session, wid, uid, {"filename": "legacy", "total_size": 5}
        )["upload_id"]
    )
    row = session.get(UploadReservation, rid)
    assert row is not None
    row.details = {
        key: value
        for key, value in row.details.items()
        if key not in {"object_name", "stage_id"}
    }
    session.commit()
    with pytest.raises(ConflictError, match="reinitialized"):
        service.complete_multipart_upload(session, wid, uid, rid)


def test_workspace_concurrency_applies_across_users(
    session: Session,
    test_workspace: Workspace,
    test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid, wid = int(test_user.id), int(test_workspace.id)
    monkeypatch.setattr(quota, "WORKSPACE_CONCURRENCY", 1)
    other = User(username="concurrent-member", password_hash="x")
    session.add(other)
    session.commit()
    quota.reserve(
        session, quota.ReserveParams(workspace_id=wid, user_id=uid, size=1)
    )
    with pytest.raises(ConflictError, match="active"):
        quota.reserve(
            session,
            quota.ReserveParams(
                workspace_id=wid, user_id=int(other.id), size=1
            ),
        )


@pytest.mark.parametrize("failure_point", ["filename", "staging", "record"])
@pytest.mark.parametrize("delete_failure", [False, True])
def test_batch_preparation_failure_compensates_previous_uploads(
    session: Session,
    test_workspace: Workspace,
    test_user: User,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_object_storage: InMemoryStorageClient,
    failure_point: str,
    delete_failure: bool,
) -> None:
    uid, wid = int(test_user.id), int(test_workspace.id)
    upload_root = tmp_path / "uploads"
    monkeypatch.setattr(upload_config, "UPLOAD_FOLDER", str(upload_root))
    monkeypatch.setattr(quota, "USER_CAPACITY", 10)
    attribute = {
        "filename": "object_name",
        "staging": "StagedUpload",
        "record": "File",
    }[failure_point]
    original = getattr(publication, attribute)
    calls = 0

    def prepare(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("private-preparation-fault")
        return original(*args, **kwargs)

    monkeypatch.setattr(publication, attribute, prepare)
    delete_func = mock_object_storage.delete_file
    if delete_failure:
        monkeypatch.setattr(
            mock_object_storage,
            "delete_file",
            MagicMock(side_effect=OSError("private-storage-fault")),
        )
    expected_message = (
        "Failed to save uploaded file metadata"
        if failure_point == "record"
        else "Failed to upload file"
    )
    with pytest.raises(ServiceOperationError, match=expected_message):
        get_upload_service().create_uploaded_files(
            session, wid, uid, [adapter(b"12345"), adapter(b"12345")]
        )
    expected_attempts = 2 if failure_point == "record" else 1
    assert session.query(File).count() == 0
    assert (
        session.query(UploadReservation).filter_by(status="active").count() == 0
    )
    assert session.query(UploadDailyUsage).one().attempts == expected_attempts
    assert list(upload_root.iterdir()) == []
    if delete_failure:
        assert len(mock_object_storage.objects) == expected_attempts
        with pytest.raises(BusinessRuleError, match="capacity"):
            quota.reserve(
                session,
                quota.ReserveParams(workspace_id=wid, user_id=uid, size=10),
            )
        session.rollback()
        monkeypatch.setattr(mock_object_storage, "delete_file", delete_func)
        quota.cleanup(
            session,
            multipart_settings().multipart_root,
            upload_root=str(upload_root),
            storage_factory=lambda: mock_object_storage,
        )
    assert mock_object_storage.objects == {}
    assert (
        quota.reserve(
            session, quota.ReserveParams(workspace_id=wid, user_id=uid, size=10)
        ).size
        == 10
    )


@pytest.mark.parametrize("kind", ["single", "batch", "multipart"])
def test_upload_commit_ack_failure_preserves_committed_objects(
    session: Session,
    test_workspace: Workspace,
    test_user: User,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_object_storage: InMemoryStorageClient,
    kind: str,
) -> None:
    uid, wid = int(test_user.id), int(test_workspace.id)
    upload_root = tmp_path / "uploads"
    monkeypatch.setattr(upload_config, "UPLOAD_FOLDER", str(upload_root))
    commit = session.commit
    acknowledged = False

    def acknowledged_commit() -> None:
        nonlocal acknowledged
        publication_committed = any(
            isinstance(row, File)
            for row in (*session.new, *session.identity_map.values())
        )
        commit()
        if publication_committed and not acknowledged:
            acknowledged = True
            raise OSError("private-commit-acknowledgement-fault")

    monkeypatch.setattr(session, "commit", acknowledged_commit)
    with pytest.raises(ServiceOperationError):
        if kind == "single":
            get_upload_service().create_uploaded_file(
                session, wid, uid, adapter(b"12345")
            )
        elif kind == "batch":
            get_upload_service().create_uploaded_files(
                session, wid, uid, [adapter(b"12345"), adapter(b"67890")]
            )
        else:
            multipart = get_multipart_service()
            rid = str(
                multipart.init_multipart_upload(
                    session, wid, uid, {"filename": "x", "total_size": 5}
                )["upload_id"]
            )
            multipart.save_multipart_chunk(
                session,
                SaveMultipartChunkParams(
                    uploader_id=uid,
                    workspace_id=wid,
                    upload_id=rid,
                    chunk_index=0,
                    chunk=adapter(b"12345"),
                ),
            )
            multipart.complete_multipart_upload(session, wid, uid, rid)

    expected = 2 if kind == "batch" else 1
    session.expire_all()
    files = session.query(File).all()
    assert len(files) == expected
    assert len(mock_object_storage.objects) == expected
    assert all(
        mock_object_storage.objects[file.file_path] in [b"12345", b"67890"]
        for file in files
    )
    rows = session.query(UploadReservation).all()
    assert len(rows) == expected and all(
        row.status == "completed" for row in rows
    )
    assert all(
        row.details["object_name"] in mock_object_storage.objects for row in rows
    )
    quota.cleanup(
        session,
        multipart_settings().multipart_root,
        upload_root=str(upload_root),
        storage_factory=lambda: mock_object_storage,
    )
    assert len(mock_object_storage.objects) == expected
    assert not (upload_root / ".staging").exists()


@pytest.mark.parametrize("failure", ["share", "profile", "ack"])
def test_avatar_publication_metadata_commits_together(
    session: Session,
    test_workspace: Workspace,
    test_user: User,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_object_storage: InMemoryStorageClient,
    failure: str,
) -> None:
    uid = int(test_user.id)
    monkeypatch.setattr(upload_config, "UPLOAD_FOLDER", str(tmp_path))
    test_user.avatar = "/existing-avatar"
    session.commit()
    if failure in ["share", "profile"]:
        target, attribute = (
            (shares, "create_share_link")
            if failure == "share"
            else (account_repository, "set_avatar")
        )
        monkeypatch.setattr(
            target,
            attribute,
            MagicMock(
                side_effect=RuntimeError("private-avatar-publication-fault")
            ),
        )
    else:
        commit = session.commit
        acknowledged = False

        def acknowledgement() -> None:
            nonlocal acknowledged
            publication_committed = any(
                isinstance(row, File)
                for row in (*session.new, *session.identity_map.values())
            )
            commit()
            if publication_committed and not acknowledged:
                acknowledged = True
                raise OSError("private-avatar-ack-fault")

        monkeypatch.setattr(session, "commit", acknowledgement)
    with pytest.raises(ServiceOperationError):
        avatar_service.AvatarUploadService(upload_settings()).upload(
            session, adapter(b"avatar"), uid
        )
    session.expire_all()
    expected = int(failure == "ack")
    assert session.query(File).count() == expected
    assert session.query(Share).count() == expected
    assert session.query(FileTaskOutbox).count() == expected
    assert len(mock_object_storage.objects) == expected
    row = session.query(UploadReservation).one()
    assert row.status == ("completed" if expected else "cancelled")
    current = session.get(User, uid)
    assert current is not None
    assert current.avatar == (
        "/api/share/" + session.query(Share).one().token
        if expected
        else "/existing-avatar"
    )
    assert session.query(UploadDailyUsage).one().attempts == 1


@pytest.mark.parametrize("business_failure", [False, True])
def test_staging_close_fault_preserves_business_outcome(
    session: Session,
    test_workspace: Workspace,
    test_user: User,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_object_storage: InMemoryStorageClient,
    business_failure: bool,
) -> None:
    from loguru import logger

    monkeypatch.setattr(upload_config, "UPLOAD_FOLDER", str(tmp_path))
    close = publication.StagedUpload.close
    closed: list[bool] = []
    private = "private-staging-close-fault"

    def fail(stage: StagedUpload) -> None:
        close(stage)
        closed.append(stage.handle.closed)
        raise OSError(private)

    monkeypatch.setattr(publication.StagedUpload, "close", fail)
    original = BusinessRuleError("original metadata rule")
    if business_failure:
        monkeypatch.setattr(
            publication.repository,
            "insert_files",
            MagicMock(side_effect=original),
        )
    records: list[dict[str, Any]] = []

    def sink_fn(message: Any) -> None:
        records.append(dict(message.record))

    sink = logger.add(sink_fn)
    try:
        if business_failure:
            with pytest.raises(BusinessRuleError) as caught:
                get_upload_service().create_uploaded_file(
                    session,
                    int(test_workspace.id),
                    int(test_user.id),
                    adapter(b"x"),
                )
            assert caught.value is original
        else:
            assert get_upload_service().create_uploaded_file(
                session, int(test_workspace.id), int(test_user.id), adapter(b"x")
            ).id
    finally:
        logger.remove(sink)
    assert closed == [True]
    assert session.query(File).count() == int(not business_failure)
    assert len(mock_object_storage.objects) == int(not business_failure)
    assert all(
        private not in str(r["message"]) and r["exception"] is None
        for r in records
    )


@pytest.mark.parametrize("loss", ["demote", "delete"])
def test_avatar_acting_admin_rechecked_after_object_put(
    session: Session,
    test_workspace: Workspace,
    test_user: User,
    admin_user: User,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_object_storage: InMemoryStorageClient,
    loss: str,
) -> None:
    uid, aid = int(test_user.id), int(admin_user.id)
    test_user.avatar = "/existing-avatar"
    session.commit()
    monkeypatch.setattr(upload_config, "UPLOAD_FOLDER", str(tmp_path))
    put = mock_object_storage.upload_file

    def lose_role(*args: Any, **kwargs: Any) -> Any:
        result = put(*args, **kwargs)
        statement = (
            delete(User) if loss == "delete" else update(User).values(role="common")
        )
        session.execute(
            statement.where(User.id == aid),
            execution_options={"synchronize_session": False},
        )
        session.commit()
        return result

    monkeypatch.setattr(mock_object_storage, "upload_file", lose_role)
    with pytest.raises(PermissionDeniedError):
        avatar_service.upload_avatar_for_user(
            session, aid, "admin", uid, adapter(b"avatar")
        )
    session.expire_all()
    current_user = session.get(User, uid)
    assert current_user is not None and current_user.avatar == "/existing-avatar"
    assert (
        session.query(File).count()
        == session.query(Share).count()
        == session.query(FileTaskOutbox).count()
        == 0
    )
    assert session.query(UploadReservation).one().status == "cancelled"
    assert session.query(UploadDailyUsage).one().attempts == 1
    assert not mock_object_storage.objects
