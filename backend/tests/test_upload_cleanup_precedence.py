"""A failed temporary-file cleanup must not replace an upload's primary error."""
import asyncio
from io import BytesIO
from types import SimpleNamespace

import pytest
from loguru import logger
from werkzeug.datastructures import FileStorage

from app.exceptions import PayloadTooLargeError
from app.infra import multipart_storage, upload_adapter


@pytest.fixture
def rejected_remove(monkeypatch):
    logs = []
    sink = logger.add(lambda message: logs.append(str(message)))
    def reject(*args): raise PermissionError('private-cleanup-path-and-password')
    monkeypatch.setattr(upload_adapter.os, 'remove', reject)
    try:
        yield logs
    finally:
        logger.remove(sink)


@pytest.mark.parametrize('kind', ['fastapi', 'werkzeug', 'legacy'])
def test_oversized_upload_remains_domain_error_when_remove_fails(tmp_path, rejected_remove, kind):
    destination = tmp_path / 'private-upload-path'
    if kind == 'fastapi':
        upload = upload_adapter.FastAPIUploadAdapter(SimpleNamespace(file=BytesIO(b'abcd'), filename='piece', content_type='text/plain'))
    elif kind == 'werkzeug':
        upload = FileStorage(stream=BytesIO(b'abcd'), filename='piece')
    else:
        upload = SimpleNamespace(save=lambda path: destination.write_bytes(b'abcd'))
    with pytest.raises(PayloadTooLargeError):
        upload_adapter.save_bounded(upload, str(destination), 3)
    assert rejected_remove and 'PermissionError' in ''.join(rejected_remove)
    assert 'private-' not in ''.join(rejected_remove)


@pytest.mark.parametrize('operation', ['metadata', 'chunk', 'assembly'])
@pytest.mark.parametrize('error_type', [OSError, KeyboardInterrupt, asyncio.CancelledError])
def test_atomic_publication_keeps_primary_exception_and_previous_bytes(tmp_path, monkeypatch, rejected_remove, operation, error_type):
    primary = error_type('private-publication-detail')
    destination = tmp_path / ('0.part' if operation == 'chunk' else 'published')
    destination.write_bytes(b'previous')
    chunks = tmp_path / 'chunks'
    chunks.mkdir()
    (chunks / '0.part').write_bytes(b'abc')
    def reject(*args): raise primary
    monkeypatch.setattr(multipart_storage.os, 'replace', reject)
    with pytest.raises(error_type) as error:
        if operation == 'metadata':
            multipart_storage.write_metadata(str(destination), {'name': 'private-name'})
        elif operation == 'chunk':
            multipart_storage.save_chunk(str(tmp_path), 0, FileStorage(stream=BytesIO(b'abc')), 3, 3)
        else:
            multipart_storage.assemble(str(chunks), str(destination), total_chunks=1, chunk_size=3, total_size=3)
    assert error.value is primary
    assert destination.read_bytes() == b'previous'
    assert rejected_remove and 'PermissionError' in ''.join(rejected_remove)
    assert 'private-' not in ''.join(rejected_remove)
