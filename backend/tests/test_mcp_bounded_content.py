"""MCP text reads bound downloaded bytes before decoding or local staging."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.mcp import server
from app.infra.storage import MinIOStorageClient
from loguru import logger


@pytest.mark.parametrize('body', [b'abcdefghijklmnopqrstuvwxyz', ('中' * 12).encode()])
async def test_content_read_bounds_object_bytes_without_full_download(monkeypatch, body):
    file = SimpleNamespace(name='text.txt', mime_type='text/plain', file_path='object')
    calls = []
    def download(name, destination):
        calls.append(('download', len(body)))
        Path(destination).write_bytes(body)
    def prefix(name, limit):
        calls.append(('prefix', limit))
        return body[:limit]
    storage = SimpleNamespace(file_exists=lambda name: True, get_file_size=lambda name: len(body),
                              download_file=download, read_file_prefix=prefix)
    monkeypatch.setattr(server, '_MAX_READ_BYTES', 16)
    monkeypatch.setattr(server, '_require_read_context', lambda: None)
    monkeypatch.setattr(server, '_get_authorized_file', lambda *args: file)
    monkeypatch.setattr(server, 'get_storage_client', lambda: storage)
    monkeypatch.setattr(server, 'SessionLocal', lambda: SimpleNamespace(close=lambda: None))
    result = json.loads(await server.read_file_content(1))
    assert calls == [('prefix', 16)]
    assert result['truncated'] and result['file_size'] == len(body)
    assert result['content'] == (body[:16].decode('utf-8', errors='ignore'))


async def test_content_storage_failure_returns_safe_tool_error(monkeypatch):
    file = SimpleNamespace(name='text.txt', mime_type='text/plain', file_path='object')
    def fail(name):
        raise OSError('private-storage-endpoint-and-credential')
    monkeypatch.setattr(server, '_require_read_context', lambda: None)
    monkeypatch.setattr(server, '_get_authorized_file', lambda *args: file)
    monkeypatch.setattr(server, 'get_storage_client', lambda: SimpleNamespace(file_exists=fail))
    monkeypatch.setattr(server, 'SessionLocal', lambda: SimpleNamespace(close=lambda: None))
    result = json.loads(await server.read_file_content(1))
    assert result == {'error': 'Service temporarily unavailable'}


@pytest.mark.parametrize('read_fails', [False, True])
@pytest.mark.parametrize('close_fails', [False, True])
def test_prefix_response_released_without_masking_read_outcome(read_fails, close_fails):
    primary = OSError('private-object-read-error')
    response = Mock()
    response.read.side_effect = primary if read_fails else None
    response.read.return_value = b'prefix'
    if close_fails:
        response.close.side_effect = OSError('private-object-close-error')
    client = MinIOStorageClient.__new__(MinIOStorageClient)
    client._bucket = 'bucket'
    client._client = Mock()
    client._client.get_object.return_value = response
    logs = []
    sink = logger.add(lambda message: logs.append(str(message)))
    try:
        if read_fails:
            with pytest.raises(OSError) as error:
                client.read_file_prefix('object', 16)
            assert error.value is primary
        else:
            assert client.read_file_prefix('object', 16) == b'prefix'
    finally:
        logger.remove(sink)
    client._client.get_object.assert_called_once_with('bucket', 'object', length=16)
    response.read.assert_called_once_with(16)
    response.close.assert_called_once()
    response.release_conn.assert_called_once()
    assert 'private-' not in ''.join(logs)
