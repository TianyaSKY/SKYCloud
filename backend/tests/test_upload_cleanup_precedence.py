"""失败的临时文件清理不得替代上传的主要错误测试."""

import asyncio
from collections.abc import Generator
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from fastapi import UploadFile
import pytest
from loguru import logger
from werkzeug.datastructures import FileStorage

from app.exceptions import PayloadTooLargeError
from app.infra import multipart_storage, upload_adapter


@pytest.fixture
def rejected_remove(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[list[str], None, None]:
    logs: list[str] = []
    sink = logger.add(lambda message: logs.append(str(message)))

    def reject(*args: Any, **kwargs: Any) -> None:
        raise PermissionError("private-cleanup-path-and-password")

    monkeypatch.setattr(multipart_storage.os, "unlink", reject)
    try:
        yield logs
    finally:
        logger.remove(sink)


@pytest.mark.parametrize("kind", ["fastapi", "werkzeug", "legacy"])
def test_oversized_upload_remains_domain_error_when_remove_fails(
    tmp_path: Path, rejected_remove: list[str], kind: str
) -> None:
    destination = tmp_path / "private-upload-path"
    if kind == "fastapi":
        upload: Any = upload_adapter.FastAPIUploadAdapter(
            cast(
                UploadFile,
                SimpleNamespace(
                    file=BytesIO(b"abcd"),
                    filename="piece",
                    content_type="text/plain",
                ),
            )
        )
    elif kind == "werkzeug":
        upload = FileStorage(stream=BytesIO(b"abcd"), filename="piece")
    else:

        def save_fn(path: str) -> None:
            destination.write_bytes(b"abcd")

        upload = SimpleNamespace(save=save_fn)
    with pytest.raises(PayloadTooLargeError):
        upload_adapter.save_bounded(upload, str(destination), 3)
    assert rejected_remove and "PermissionError" in "".join(rejected_remove)
    assert "private-" not in "".join(rejected_remove)


@pytest.mark.parametrize("operation", ["metadata", "chunk", "assembly"])
@pytest.mark.parametrize(
    "error_type", [OSError, KeyboardInterrupt, asyncio.CancelledError]
)
def test_atomic_publication_keeps_primary_exception_and_previous_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rejected_remove: list[str],
    operation: str,
    error_type: type[BaseException],
) -> None:
    primary = error_type("private-publication-detail")
    destination = tmp_path / ("0.part" if operation == "chunk" else "published")
    destination.write_bytes(b"previous")
    chunks = tmp_path / "chunks"
    chunks.mkdir()
    (chunks / "0.part").write_bytes(b"abc")

    def reject(*args: Any, **kwargs: Any) -> None:
        raise primary

    monkeypatch.setattr(multipart_storage.os, "replace", reject)
    with pytest.raises(error_type) as error:
        if operation == "metadata":
            multipart_storage.write_metadata(
                str(destination), {"name": "private-name"}
            )
        elif operation == "chunk":
            multipart_storage.save_chunk(
                str(tmp_path),
                multipart_storage.SaveChunkParams(
                    index=0,
                    upload=FileStorage(stream=BytesIO(b"abc")),
                    expected_size=3,
                    max_size=3,
                ),
            )
        else:
            multipart_storage.assemble(
                str(chunks),
                str(destination),
                multipart_storage.AssembleParams(
                    total_chunks=1, chunk_size=3, total_size=3
                ),
            )
    assert error.value is primary
    assert destination.read_bytes() == b"previous"
    assert rejected_remove and "PermissionError" in "".join(rejected_remove)
    assert "private-" not in "".join(rejected_remove)
