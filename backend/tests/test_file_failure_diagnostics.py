"""存储/文件/全局/库诊断信息不能渲染私有异常数据测试."""
from io import BytesIO
import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from minio.error import S3Error
import pytest
from urllib3.response import HTTPResponse

from app.exceptions import ServiceOperationError, register_exception_handlers
from app.infra.storage import MinIOStorageClient
from tests.test_llm_safe_diagnostics import PRIVATE, assert_safe, logs

_ = logs


@pytest.mark.parametrize('method,args,sdk_method', [
    ('_ensure_bucket', (), 'bucket_exists'),
    ('upload_file', (PRIVATE, PRIVATE), 'fput_object'),
    ('upload_data', (b'private', PRIVATE), 'put_object'),
    ('upload_stream', (BytesIO(b'private'), PRIVATE, 7), 'put_object'),
    ('download_file', (PRIVATE, PRIVATE), 'fget_object'),
    ('get_file_stream', (PRIVATE,), 'get_object'),
    ('delete_file', (PRIVATE,), 'remove_object'),
    ('file_exists', (PRIVATE,), 'stat_object'),
    ('get_file_size', (PRIVATE,), 'stat_object'),
])
def test_storage_failures_propagate_without_serializing_object_or_s3_response(logs: list[tuple[str, str]], method: str, args: tuple[Any, ...], sdk_method: str) -> None:
    storage = MinIOStorageClient.__new__(MinIOStorageClient)
    storage._bucket = PRIVATE  # pyright: ignore[reportPrivateUsage]
    storage._client = Mock()  # pyright: ignore[reportPrivateUsage]
    error = S3Error('AccessDenied', PRIVATE, PRIVATE, PRIVATE, PRIVATE, None)  # type: ignore[arg-type] # pyright: ignore[reportArgumentType]
    getattr(storage._client, sdk_method).side_effect = error  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(S3Error) as caught:
        getattr(storage, method)(*args)
    assert caught.value is error
    assert_safe(logs, 'error_type=S3Error')


def test_download_cleanup_returns_connection_even_when_close_fails(logs: list[tuple[str, str]]) -> None:
    stream = Mock()
    stream.close.side_effect = RuntimeError(PRIVATE)
    MinIOStorageClient.close_file_stream(stream)
    stream.close.assert_called_once()
    stream.release_conn.assert_called_once()
    assert_safe(logs, 'error_type=RuntimeError')


def test_read_file_prefix_preserves_bytes_when_connection_release_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, logs: list[tuple[str, str]],
) -> None:
    payload = tmp_path / "object.bin"
    payload.write_bytes(b"downloaded payload")
    storage = MinIOStorageClient.__new__(MinIOStorageClient)
    storage._bucket = "test-bucket"  # pyright: ignore[reportPrivateUsage]
    with payload.open("rb") as body:
        response = HTTPResponse(body=body, preload_content=False)
        storage._client = Mock()  # pyright: ignore[reportPrivateUsage]
        storage._client.get_object.return_value = response  # pyright: ignore[reportPrivateUsage]

        def fail_release() -> None:
            raise RuntimeError(PRIVATE)

        monkeypatch.setattr(response, "release_conn", fail_release)
        assert storage.read_file_prefix("object.bin", 10) == b"downloaded"
        assert body.closed
    assert_safe(logs, "error_type=RuntimeError")


def test_read_file_prefix_preserves_read_failure_when_connection_release_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, logs: list[tuple[str, str]],
) -> None:
    payload = tmp_path / "object.bin"
    payload.write_bytes(b"downloaded payload")
    storage = MinIOStorageClient.__new__(MinIOStorageClient)
    storage._bucket = "test-bucket"  # pyright: ignore[reportPrivateUsage]
    original = LookupError(PRIVATE)
    with payload.open("rb") as body:
        response = HTTPResponse(body=body, preload_content=False)
        storage._client = Mock()  # pyright: ignore[reportPrivateUsage]
        storage._client.get_object.return_value = response  # pyright: ignore[reportPrivateUsage]

        def fail_read(*args: Any, **kwargs: Any) -> None:
            raise original

        def fail_release() -> None:
            raise RuntimeError(PRIVATE)

        monkeypatch.setattr(body, "read", fail_read)
        monkeypatch.setattr(response, "release_conn", fail_release)
        with pytest.raises(LookupError) as caught:
            storage.read_file_prefix("object.bin", 10)
        assert caught.value is original
        assert body.closed
    assert_safe(logs, "error_type=RuntimeError")


def test_stdlib_exception_record_omits_message_and_traceback_but_keeps_normal_messages(logs: list[tuple[str, str]]) -> None:
    from app.infra.logging_setup import InterceptHandler
    library = logging.getLogger('private-test-library')
    prior = library.handlers, library.propagate, library.level
    library.handlers = [InterceptHandler()]
    library.propagate = False
    library.setLevel(logging.DEBUG)
    try:
        library.info('Ordinary library diagnostic')
        try:
            raise ValueError(PRIVATE)
        except ValueError:
            library.exception('Provider response: %s', PRIVATE)
    finally:
        library.handlers, library.propagate, library.level = prior
    assert any('Ordinary library diagnostic' in text for text, _ in logs)
    assert_safe(logs, 'error_type=ValueError')


@pytest.mark.parametrize('kind', ['domain', 'http', 'unexpected'])
def test_global_500_response_and_log_diagnostics_are_safe(logs: list[tuple[str, str]], kind: str) -> None:
    app = FastAPI()
    register_exception_handlers(app)
    @app.get('/failure')
    def failure() -> None:
        if kind == 'domain':
            raise ServiceOperationError(PRIVATE)
        if kind == 'http':
            raise HTTPException(502, detail=PRIVATE)
        raise RuntimeError(PRIVATE)
    response = TestClient(app, raise_server_exceptions=False).get('/failure')
    assert response.status_code >= 500 and response.json()['error']['message'] == 'Internal server error'
    assert PRIVATE not in response.text
    assert_safe(logs, 'status=502' if kind == 'http' else 'error_type=')


async def test_file_embedding_failure_returns_empty_vector_with_private_exception_removed(monkeypatch: pytest.MonkeyPatch, logs: list[tuple[str, str]]) -> None:
    from app.features.file import search_service
    monkeypatch.setattr(search_service, 'embed_texts', AsyncMock(side_effect=RuntimeError(PRIVATE)))
    assert await search_service.embedding_desc(PRIVATE, {'api': 'unused', 'key': PRIVATE}) == []
    assert_safe(logs, 'error_type=RuntimeError')


@pytest.mark.parametrize("invalidation_fails", [False, True])
def test_upload_rollback_cleanup_preserves_original_failure(
    logs: list[tuple[str, str]], invalidation_fails: bool,
) -> None:
    from app.features.file.upload_recovery import compensate_failed_uploads

    session = Mock()
    session.rollback.side_effect = RuntimeError(PRIVATE)
    if invalidation_fails:
        session.invalidate.side_effect = RuntimeError(PRIVATE)
    storage = Mock()
    original = LookupError("upload failed")
    with pytest.raises(LookupError) as caught:
        try:
            raise original
        except LookupError:
            compensate_failed_uploads(session, 1, 1, [], storage)
            raise
    assert caught.value is original
    session.invalidate.assert_called_once_with()
    storage.delete_file.assert_not_called()
    assert_safe(logs, "error_type=RuntimeError")

