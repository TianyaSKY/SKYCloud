"""真实文件系统故障边界: 失败写入必须保留已发布字节测试."""
from io import BytesIO
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy.orm import Session
from werkzeug.datastructures import FileStorage

from app.models import User, Workspace
from app.exceptions import BusinessRuleError, PayloadTooLargeError
from app.features.file import upload_config, upload_quota
from app.features.file.multipart_service import (
    MultipartInitData,
    SaveMultipartChunkParams,
    get_multipart_service,
)
from app.features.file.upload_types import UploadSource
from app.infra import multipart_storage as storage
from app.infra.upload_staging import StagedUpload


def upload(data: bytes) -> FileStorage:
    return FileStorage(stream=BytesIO(data), filename="piece")


@pytest.mark.parametrize("operation", ["metadata", "chunk", "assembly"])
def test_failed_publication_retains_previous_file_and_removes_temporary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    destination = tmp_path / ("0.part" if operation == "chunk" else "published")
    destination.write_bytes(b"previous")
    chunks = tmp_path / "chunks"
    chunks.mkdir()
    (chunks / "0.part").write_bytes(b"abc")
    def reject_replace(*args: Any, **kwargs: Any) -> None:
        raise OSError("injected disk publication failure")
    monkeypatch.setattr(storage.os, "replace", reject_replace)
    with pytest.raises(OSError, match="publication failure"):
        if operation == "metadata":
            storage.write_metadata(str(destination), {"filename": "中文"})
        elif operation == "chunk":
            storage.save_chunk(str(tmp_path), storage.SaveChunkParams(index=0, upload=upload(b"abc"), expected_size=3, max_size=3))
        else:
            storage.assemble(str(chunks), str(destination), storage.AssembleParams(total_chunks=1, chunk_size=3, total_size=3))
    assert destination.read_bytes() == b"previous"
    assert not list(tmp_path.rglob("*.tmp"))


@pytest.mark.parametrize("data,error", [(b"ab", BusinessRuleError), (b"abcd", PayloadTooLargeError)])
def test_retry_with_wrong_size_keeps_valid_chunk(tmp_path: Path, data: bytes, error: type[Exception]) -> None:
    storage.save_chunk(str(tmp_path), storage.SaveChunkParams(index=0, upload=upload(b"abc"), expected_size=3, max_size=3))
    with pytest.raises(error):
        storage.save_chunk(str(tmp_path), storage.SaveChunkParams(index=0, upload=upload(data), expected_size=3, max_size=3))
    assert (tmp_path / "0.part").read_bytes() == b"abc"
    assert storage.list_chunks(str(tmp_path)) == [0]
    assert not list(tmp_path.glob("*.tmp"))


def test_interrupted_merge_removes_partial_file_and_keeps_chunks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    chunks = tmp_path / "chunks"
    storage.save_chunk(str(chunks), storage.SaveChunkParams(index=0, upload=upload(b"abc"), expected_size=3, max_size=3))
    destination = tmp_path / "published"
    destination.write_bytes(b"previous")
    def interrupt(source: Any, target: Any, limit: Any) -> None:
        target.write(b"partial")
        raise KeyboardInterrupt()
    monkeypatch.setattr(storage, "copy_bounded", interrupt)
    with pytest.raises(KeyboardInterrupt):
        storage.assemble(str(chunks), str(destination), storage.AssembleParams(total_chunks=1, chunk_size=3, total_size=3))
    assert destination.read_bytes() == b"previous"
    assert (chunks / "0.part").read_bytes() == b"abc"
    assert not list(tmp_path.rglob("*.tmp"))


def test_real_merge_orders_chunks_and_checks_persisted_sizes(tmp_path: Path) -> None:
    chunks = tmp_path / "chunks"
    storage.save_chunk(str(chunks), storage.SaveChunkParams(index=1, upload=upload(b"de"), expected_size=2, max_size=3))
    storage.save_chunk(str(chunks), storage.SaveChunkParams(index=0, upload=upload(b"abc"), expected_size=3, max_size=3))
    destination = tmp_path / "published"
    storage.assemble(str(chunks), str(destination), storage.AssembleParams(total_chunks=2, chunk_size=3, total_size=5))
    assert destination.read_bytes() == b"abcde"
    (chunks / "0.part").write_bytes(b"ab")
    with pytest.raises(BusinessRuleError):
        storage.assemble(str(chunks), str(destination), storage.AssembleParams(total_chunks=2, chunk_size=3, total_size=5))
    assert destination.read_bytes() == b"abcde"
    assert not list(tmp_path.rglob("*.tmp"))


def test_service_merge_interruption_releases_staging_lease(session: Session, test_workspace: Workspace, test_user: User, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    upload_root = tmp_path / "uploads"
    monkeypatch.setattr(upload_config, "UPLOAD_FOLDER", str(upload_root))
    service = get_multipart_service()
    upload_id = "interrupted-upload"
    service.init_multipart_upload(session, test_workspace.id, test_user.id,
                                 cast(MultipartInitData, {"filename": "test.txt", "total_size": 3,
                                  "chunk_size": 3, "upload_id": upload_id}))
    service.save_multipart_chunk(session, SaveMultipartChunkParams(uploader_id=test_user.id, workspace_id=test_workspace.id, upload_id=upload_id, chunk_index=0, chunk=cast(UploadSource, upload(b"abc"))))
    reservation = upload_quota.find_reservation(session, upload_id)
    assert reservation is not None
    assert reservation.details is not None
    stage_id = cast(str, reservation.details["stage_id"])
    def interrupt(source: Any, target: Any, limit: Any) -> None:
        target.write(b"partial")
        raise KeyboardInterrupt()
    monkeypatch.setattr(storage, "copy_bounded", interrupt)
    with pytest.raises(KeyboardInterrupt):
        service.complete_multipart_upload(session, test_workspace.id, test_user.id, upload_id)
    assert not (upload_root / ".staging").exists()
    chunks = Path(storage.chunks_directory(service.directory(test_workspace.id, upload_id)))
    assert (chunks / "0.part").read_bytes() == b"abc"
    assert reservation.status == "active"
    retry = StagedUpload(str(upload_root), "test.txt", stage_id=stage_id)
    retry.close()

