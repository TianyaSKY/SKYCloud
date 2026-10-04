import asyncio
import base64
from pathlib import Path
import tempfile

import httpx
from io import BytesIO
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, File, UploadFile
from fastapi.testclient import TestClient
from werkzeug.datastructures import FileStorage

from app.exceptions import PayloadTooLargeError
from app.infra.upload_adapter import Base64UploadAdapter, FastAPIUploadAdapter, save_bounded
from app.infra.upload_limits import UploadBodyLimitMiddleware, positive_limit


@pytest.mark.parametrize('kind', ['http', 'base64', 'werkzeug'])
def test_bounded_adapter_accepts_exact_limit_and_removes_oversized_file(tmp_path, kind):
    def adapter(data):
        if kind == 'http':
            return FastAPIUploadAdapter(UploadFile(filename='x', file=BytesIO(data)))
        if kind == 'base64':
            return Base64UploadAdapter(base64.b64encode(data).decode(), 'x')
        return FileStorage(stream=BytesIO(data), filename='x')
    path = str(tmp_path / 'x')
    save_bounded(adapter(b'12345'), path, 5)
    assert (tmp_path / 'x').read_bytes() == b'12345'
    (tmp_path / 'x').unlink()
    with pytest.raises(PayloadTooLargeError):
        save_bounded(adapter(b'123456'), path, 5)
    assert not (tmp_path / 'x').exists()


def test_stream_stops_at_one_extra_byte(tmp_path):
    source = BytesIO(b'x' * 100000)
    with pytest.raises(PayloadTooLargeError):
        FastAPIUploadAdapter(UploadFile(filename='x', file=source)).save(str(tmp_path / 'x'), 10)
    assert source.tell() == 11
    assert not (tmp_path / 'x').exists()


@pytest.mark.parametrize('length_header', [True, False])
def test_request_limit_checks_actual_body_before_route(length_header):
    app = FastAPI()
    app.add_middleware(UploadBodyLimitMiddleware, max_bytes=150)
    calls = []

    @app.post('/api/files')
    def upload(file: UploadFile = File(...)):
        calls.append(True)
        return {'ok': True}

    boundary = 'upload-test'
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="x"\r\n\r\n'.encode()
            + b'x' * 1000 + f'\r\n--{boundary}--\r\n'.encode())
    headers = {'content-type': f'multipart/form-data; boundary={boundary}'}
    content = body if length_header else iter([body[:100], body[100:]])
    response = TestClient(app).post('/api/files', content=content, headers=headers)
    assert response.status_code == 413
    assert calls == []


def test_single_file_excess_is_413_without_storage_or_temporary_file(session, tmp_path, monkeypatch):
    from app.features.file import service
    storage = MagicMock()
    monkeypatch.setattr(service, 'UPLOAD_FOLDER', str(tmp_path))
    monkeypatch.setattr(service, 'MAX_UPLOAD_SIZE', 5)
    monkeypatch.setattr(service, 'get_storage_client', lambda: storage)
    upload = FastAPIUploadAdapter(UploadFile(filename='x', file=BytesIO(b'123456')))
    with pytest.raises(PayloadTooLargeError):
        service.create_file(session, upload, {'workspace_id': 1, 'uploader_id': 1})
    storage.upload_file.assert_not_called()
    assert list(tmp_path.iterdir()) == []


def test_oversized_part_preserves_existing_part_and_cleans_temp(session, test_workspace, test_user, tmp_path, monkeypatch):
    from app.features.file import service
    monkeypatch.setattr(service, 'MULTIPART_ROOT', str(tmp_path))
    # Use the service's path contract rather than assume directory naming.
    result = service.init_multipart_upload(session, test_workspace.id, test_user.id,
        {'filename': 'x', 'total_size': 5, 'chunk_size': 5, 'upload_id': 'abcdefgh'})
    directory = Path(service._multipart_upload_dir(test_workspace.id, result['upload_id']))
    chunks = directory / 'chunks'
    (chunks / '0.part').write_bytes(b'12345')
    adapter = FastAPIUploadAdapter(UploadFile(filename='x', file=BytesIO(b'123456')))
    with pytest.raises(PayloadTooLargeError):
        service.save_multipart_chunk(session, test_user.id, test_workspace.id, 'abcdefgh', 0, adapter)
    assert (chunks / '0.part').read_bytes() == b'12345'
    assert list(chunks.glob('*.tmp')) == []


@pytest.mark.parametrize('value', ['0', '-1', 'invalid'])
def test_upload_limit_cannot_disable_protection(monkeypatch, value):
    monkeypatch.setenv('TEST_UPLOAD_LIMIT', value)
    with pytest.raises(RuntimeError):
        positive_limit('TEST_UPLOAD_LIMIT', 100)


def test_chunked_request_closes_already_open_multipart_spools(monkeypatch):
    app = FastAPI()
    app.add_middleware(UploadBodyLimitMiddleware, max_bytes=300)
    opened = []
    def spool(*args, **kwargs):
        file = tempfile.SpooledTemporaryFile(*args, **kwargs)
        opened.append(file)
        return file
    monkeypatch.setattr('starlette.formparsers.SpooledTemporaryFile', spool)

    @app.post('/api/files')
    def upload(file: UploadFile = File(...)):
        pytest.fail('Oversized request reached upload route')

    async def body():
        yield b'--abc\r\nContent-Disposition: form-data; name="file"; filename="x"\r\n\r\n'
        yield b'x' * 100
        yield b'x' * 1000
        pytest.fail('Server continued consuming oversized request')

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            return await client.post('/api/files', content=body(), headers={'content-type': 'multipart/form-data; boundary=abc'})
    response = asyncio.run(run())
    assert response.status_code == 413
    assert len(opened) == 1
    assert opened[0].closed


def test_tampered_stored_chunk_cannot_overrun_assembly(session, test_workspace, tmp_path, monkeypatch):
    from app.features.file import service
    monkeypatch.setattr(service, 'MULTIPART_ROOT', str(tmp_path / 'multipart'))
    monkeypatch.setattr(service, 'UPLOAD_FOLDER', str(tmp_path / 'uploads'))
    result = service.init_multipart_upload(session, test_workspace.id, 1, {'filename': 'x', 'total_size': 5, 'chunk_size': 5})
    upload_id = result['upload_id']
    directory = Path(service._multipart_upload_dir(test_workspace.id, upload_id))
    (directory / 'chunks' / '0.part').write_bytes(b'x' * 100000)
    storage = MagicMock()
    monkeypatch.setattr(service, 'get_storage_client', lambda: storage)
    with pytest.raises(PayloadTooLargeError):
        service.complete_multipart_upload(session, test_workspace.id, 1, upload_id)
    storage.upload_file.assert_not_called()
    assert list((tmp_path / 'uploads').iterdir()) == []


def test_upload_request_concurrency_is_bounded_and_released(monkeypatch):
    from app.infra import upload_limits
    monkeypatch.setattr(upload_limits, 'REQUEST_CONCURRENCY', 1)
    async def run():
        entered = asyncio.Event()
        finish = asyncio.Event()
        async def app(scope, receive, send):
            entered.set()
            await finish.wait()
        middleware = UploadBodyLimitMiddleware(app)
        scope = {'type': 'http', 'method': 'POST', 'path': '/api/files', 'headers': []}
        responses = []
        async def receive():
            return {'type': 'http.request', 'body': b'', 'more_body': False}
        async def send(message):
            responses.append(message)
        first = asyncio.create_task(middleware(scope, receive, send))
        await entered.wait()
        await middleware(scope, receive, send)
        assert responses[0]['status'] == 429
        finish.set()
        await first
        assert middleware.active_requests == 0
        await middleware(scope, receive, send)
        assert middleware.active_requests == 0
    asyncio.run(run())
