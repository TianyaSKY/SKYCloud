"""Real filesystem fault boundaries: failed writes must retain published bytes."""
from io import BytesIO

import pytest
from werkzeug.datastructures import FileStorage

from app.exceptions import BusinessRuleError, PayloadTooLargeError
from app.infra import multipart_storage as storage


def upload(data):
    return FileStorage(stream=BytesIO(data), filename="piece")


@pytest.mark.parametrize("operation", ["metadata", "chunk", "assembly"])
def test_failed_publication_retains_previous_file_and_removes_temporary(tmp_path, monkeypatch, operation):
    destination = tmp_path / ("0.part" if operation == "chunk" else "published")
    destination.write_bytes(b"previous")
    chunks = tmp_path / "chunks"
    chunks.mkdir()
    (chunks / "0.part").write_bytes(b"abc")
    def reject_replace(*args, **kwargs):
        raise OSError("injected disk publication failure")
    monkeypatch.setattr(storage.os, "replace", reject_replace)
    with pytest.raises(OSError, match="publication failure"):
        if operation == "metadata":
            storage.write_metadata(str(destination), {"filename": "中文"})
        elif operation == "chunk":
            storage.save_chunk(str(tmp_path), 0, upload(b"abc"), 3, 3)
        else:
            storage.assemble(str(chunks), str(destination), total_chunks=1, chunk_size=3, total_size=3)
    assert destination.read_bytes() == b"previous"
    assert not list(tmp_path.rglob("*.tmp"))


@pytest.mark.parametrize("data,error", [(b"ab", BusinessRuleError), (b"abcd", PayloadTooLargeError)])
def test_retry_with_wrong_size_keeps_valid_chunk(tmp_path, data, error):
    storage.save_chunk(str(tmp_path), 0, upload(b"abc"), 3, 3)
    with pytest.raises(error):
        storage.save_chunk(str(tmp_path), 0, upload(data), 3, 3)
    assert (tmp_path / "0.part").read_bytes() == b"abc"
    assert storage.list_chunks(str(tmp_path)) == [0]
    assert not list(tmp_path.glob("*.tmp"))


def test_interrupted_merge_removes_partial_file_and_keeps_chunks(tmp_path, monkeypatch):
    chunks = tmp_path / "chunks"
    storage.save_chunk(str(chunks), 0, upload(b"abc"), 3, 3)
    destination = tmp_path / "published"
    destination.write_bytes(b"previous")
    def interrupt(source, target, limit):
        target.write(b"partial")
        raise KeyboardInterrupt()
    monkeypatch.setattr(storage, "copy_bounded", interrupt)
    with pytest.raises(KeyboardInterrupt):
        storage.assemble(str(chunks), str(destination), total_chunks=1, chunk_size=3, total_size=3)
    assert destination.read_bytes() == b"previous"
    assert (chunks / "0.part").read_bytes() == b"abc"
    assert not list(tmp_path.rglob("*.tmp"))


def test_real_merge_orders_chunks_and_checks_persisted_sizes(tmp_path):
    chunks = tmp_path / "chunks"
    storage.save_chunk(str(chunks), 1, upload(b"de"), 2, 3)
    storage.save_chunk(str(chunks), 0, upload(b"abc"), 3, 3)
    destination = tmp_path / "published"
    storage.assemble(str(chunks), str(destination), total_chunks=2, chunk_size=3, total_size=5)
    assert destination.read_bytes() == b"abcde"
    (chunks / "0.part").write_bytes(b"ab")
    with pytest.raises(BusinessRuleError):
        storage.assemble(str(chunks), str(destination), total_chunks=2, chunk_size=3, total_size=5)
    assert destination.read_bytes() == b"abcde"
    assert not list(tmp_path.rglob("*.tmp"))


def test_service_merge_interruption_releases_staging_lease(session, test_workspace, test_user, tmp_path, monkeypatch):
    from app.features.file import service
    from app.infra.upload_staging import StagedUpload
    from pathlib import Path

    monkeypatch.setattr(service, "MULTIPART_ROOT", str(tmp_path / "parts"))
    monkeypatch.setattr(service, "UPLOAD_FOLDER", str(tmp_path / "uploads"))
    upload_id = "interrupted-upload"
    service.init_multipart_upload(session, test_workspace.id, test_user.id,
                                 {"filename": "test.txt", "total_size": 3,
                                  "chunk_size": 3, "upload_id": upload_id})
    service.save_multipart_chunk(session, test_user.id, test_workspace.id,
                                 upload_id, 0, upload(b"abc"))
    reservation = service.upload_quota.find_reservation(session, upload_id)
    stage_id = reservation.details["stage_id"]
    def interrupt(source, target, limit):
        target.write(b"partial")
        raise KeyboardInterrupt()
    monkeypatch.setattr(storage, "copy_bounded", interrupt)
    with pytest.raises(KeyboardInterrupt):
        service.complete_multipart_upload(session, test_workspace.id, test_user.id, upload_id)
    assert not (tmp_path / "uploads" / ".staging").exists()
    chunks = Path(service._multipart_upload_dir(test_workspace.id, upload_id)) / "chunks"
    assert (chunks / "0.part").read_bytes() == b"abc"
    assert reservation.status == "active"
    retry = StagedUpload(str(tmp_path / "uploads"), "test.txt", stage_id=stage_id)
    retry.close()
