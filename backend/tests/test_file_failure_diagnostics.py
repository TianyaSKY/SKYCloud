"""Storage/file/global/library diagnostics cannot render private exception data."""
import logging
from io import BytesIO
from unittest.mock import Mock,AsyncMock
import pytest
from fastapi import FastAPI,HTTPException
from fastapi.testclient import TestClient
from minio.error import S3Error
from app.exceptions import register_exception_handlers,ServiceOperationError
from app.infra.storage import MinIOStorageClient
from tests.test_llm_safe_diagnostics import logs,assert_safe,PRIVATE

@pytest.mark.parametrize('method,args,sdk_method',[
    ('_ensure_bucket',(),'bucket_exists'),
    ('upload_file',(PRIVATE,PRIVATE),'fput_object'),
    ('upload_data',(b'private',PRIVATE),'put_object'),
    ('upload_stream',(BytesIO(b'private'),PRIVATE,7),'put_object'),
    ('download_file',(PRIVATE,PRIVATE),'fget_object'),
    ('get_file_stream',(PRIVATE,),'get_object'),
    ('delete_file',(PRIVATE,),'remove_object'),
    ('file_exists',(PRIVATE,),'stat_object'),
    ('get_file_size',(PRIVATE,),'stat_object'),
])
def test_storage_failures_propagate_without_serializing_object_or_s3_response(logs,method,args,sdk_method):
    storage=MinIOStorageClient.__new__(MinIOStorageClient)
    storage._bucket=PRIVATE;storage._client=Mock()
    error=S3Error('AccessDenied',PRIVATE,PRIVATE,PRIVATE,PRIVATE,None)
    getattr(storage._client,sdk_method).side_effect=error
    with pytest.raises(S3Error) as caught:getattr(storage,method)(*args)
    assert caught.value is error
    assert_safe(logs,'error_type=S3Error')


def test_download_cleanup_returns_connection_even_when_close_fails(logs):
    stream=Mock();stream.close.side_effect=RuntimeError(PRIVATE)
    MinIOStorageClient.close_file_stream(stream)
    stream.close.assert_called_once();stream.release_conn.assert_called_once()
    assert_safe(logs,'error_type=RuntimeError')


def test_stdlib_exception_record_omits_message_and_traceback_but_keeps_normal_messages(logs):
    from app.infra.logging_setup import InterceptHandler
    library=logging.getLogger('private-test-library')
    prior=library.handlers,library.propagate,library.level
    library.handlers=[InterceptHandler()];library.propagate=False;library.setLevel(logging.DEBUG)
    try:
        library.info('Ordinary library diagnostic')
        try:raise ValueError(PRIVATE)
        except ValueError:library.exception('Provider response: %s',PRIVATE)
    finally:library.handlers,library.propagate,library.level=prior
    assert any('Ordinary library diagnostic' in text for text,_ in logs)
    assert_safe(logs,'error_type=ValueError')

@pytest.mark.parametrize('kind',['domain','http','unexpected'])
def test_global_500_response_and_log_diagnostics_are_safe(logs,kind):
    app=FastAPI();register_exception_handlers(app)
    @app.get('/failure')
    def failure():
        if kind=='domain':raise ServiceOperationError(PRIVATE)
        if kind=='http':raise HTTPException(502,detail=PRIVATE)
        raise RuntimeError(PRIVATE)
    response=TestClient(app,raise_server_exceptions=False).get('/failure')
    assert response.status_code>=500 and response.json()['error']['message']=='Internal server error'
    assert PRIVATE not in response.text
    assert_safe(logs,'status=502' if kind=='http' else 'error_type=')

async def test_file_embedding_failure_returns_empty_vector_with_private_exception_removed(monkeypatch,logs):
    from app.features.file import service
    from app.infra.llm import client
    monkeypatch.setattr(client,'embed_texts',AsyncMock(side_effect=RuntimeError(PRIVATE)))
    assert await service.embedding_desc(PRIVATE,{'api':'unused','key':PRIVATE})==[]
    assert_safe(logs,'error_type=RuntimeError')
