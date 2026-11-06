"""文件/文件夹 JSON、分块上传及未修改下载字节的 HTTP 契约测试."""
from io import BytesIO
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.api.dependencies import get_current_user, get_current_workspace
from app.exceptions import register_exception_handlers
from app.features.file import router as files
from app.features.folder import router as folders
from app.infra.extensions import SessionFactory, get_db, get_session_factory
from app.models import File, Folder, User, Workspace


@pytest.fixture
def contract(session: Session, test_user: User, test_workspace: Workspace) -> tuple[TestClient, File, Folder]:
    root = session.query(Folder).filter_by(workspace_id=test_workspace.id, parent_id=None).one()
    row = File(name='你好.txt', file_path='private-object-key', file_size=2,
               workspace_id=test_workspace.id, uploader_id=test_user.id, parent_id=root.id)
    session.add(row); session.commit()
    app = FastAPI(); register_exception_handlers(app)
    app.include_router(files.router); app.include_router(folders.router)
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_session_factory] = lambda: cast(SessionFactory, sessionmaker(bind=session.get_bind(), join_transaction_mode="create_savepoint"))
    app.dependency_overrides[get_current_user] = lambda: test_user
    app.dependency_overrides[get_current_workspace] = lambda: test_workspace
    return TestClient(app, raise_server_exceptions=False), row, root


def data(response: Any, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    payload: dict[str, Any] = response.json()
    assert set(payload) == {'success', 'data'} and payload['success'] is True
    return payload['data']


def test_directory_search_metadata_and_processing_use_typed_envelopes(contract: tuple[TestClient, File, Folder]) -> None:
    client, row, root = contract
    assert data(client.get(f'/files/{row.id}'))['id'] == row.id
    listing = data(client.get('/files/list', params={'parent_id': root.id}))
    assert listing['files']['items'][0]['id'] == row.id
    assert listing['files']['total'] == 1
    assert data(client.get('/files/search', params={'q': '你好', 'type': 'fuzzy'}))['total'] == 1
    assert data(client.get('/files/search'))['items'] == []
    assert data(client.get('/files/process_status')) == {'处理中': 1, '成功': 0, '失败': 0}
    assert data(client.get('/folder/root_id')) == {'root_folder_id': root.id}
    assert data(client.get(f'/folder/{root.id}'))['path'] == '/'


def test_folder_length_crud_matches_database_column(contract: tuple[TestClient, File, Folder]) -> None:
    client, _row, root = contract
    created = data(client.post('/folder', json={'name': 'x'*128}), 201)
    assert created['name'] == 'x'*128 and created['parent_id'] == root.id
    assert client.post('/folder', json={'name': 'x'*129}).status_code == 422
    assert client.put(f"/folder/{created['id']}", json={'name': 'x'*129}).status_code == 422
    assert data(client.put(f"/folder/{created['id']}", json={'name': 'renamed'}))['name'] == 'renamed'
    assert any(f['id'] == created['id'] for f in data(client.get('/folder/all'))['folders'])
    deleted = client.delete(f"/folder/{created['id']}")
    assert deleted.status_code == 204 and deleted.content == b''


def test_search_failure_is_canonical_error_and_not_empty_success(contract: tuple[TestClient, File, Folder], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _row, _root = contract
    monkeypatch.setattr(files.search_service, 'search_files', AsyncMock(return_value={'items': [], 'total': 0, 'error': 'private-provider-details'}))
    response = client.get('/files/search', params={'q': 'query', 'type': 'semantic'})
    assert response.status_code == 500
    assert response.json() == {'success': False, 'error': {'code': 'INTERNAL_ERROR', 'message': 'Internal server error'}}
    assert 'private' not in response.text


def test_malformed_avatar_json_is_rejected_without_echoing_body(contract: tuple[TestClient, File, Folder]) -> None:
    client, _, _ = contract
    response = client.post(
        '/files/upload/avatar/1',
        content=b'{"avatar":"private',
        headers={'content-type': 'application/json'},
    )
    assert response.status_code == 400
    assert response.json()['error']['message'] == 'No avatar data'
    assert 'private' not in response.text


def test_download_preserves_bytes_disposition_and_stream_cleanup(contract: tuple[TestClient, File, Folder], monkeypatch: pytest.MonkeyPatch) -> None:
    client, row, _root = contract
    stream = BytesIO(b'ab')
    storage = Mock(); storage.get_file_stream.return_value = stream
    monkeypatch.setattr(files, 'get_storage_client', lambda: storage)
    def mock_file(*args: Any) -> File:
        return row
    monkeypatch.setattr(files.file_service, 'get_downloadable_file', mock_file)
    response = client.get(f'/files/{row.id}/download')
    assert response.status_code == 200 and response.content == b'ab'
    assert "filename*=UTF-8''%E4%BD%A0%E5%A5%BD.txt" in response.headers['content-disposition']
    storage.close_file_stream.assert_called_once_with(stream)


def test_openapi_documents_json_envelopes_binary_and_no_content(contract: tuple[TestClient, File, Folder]) -> None:
    client, _row, _root = contract
    schema: Any = client.get('/openapi.json').json()
    paths: dict[str, Any] = schema['paths']
    for path, operations in paths.items():
        ops: dict[str, Any] = operations
        for operation in ops.values():
            responses: dict[str, Any] = operation['responses']
            if '204' in responses:
                assert 'content' not in responses['204']
            elif path.endswith('/download'):
                assert 'application/json' not in responses['200'].get('content', {})
            else:
                code = '201' if '201' in responses else '200'
                ref: str = responses[code]['content']['application/json']['schema']['$ref']
                assert schema['components']['schemas'][ref.split('/')[-1]]['properties']['success']['const'] is True


def test_missing_root_cannot_create_a_second_root(contract: tuple[TestClient, File, Folder], session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.features.folder import repository
    client, _row, _root = contract
    before = session.query(Folder).count()
    def mock_root_id(*args: Any) -> None:
        return None
    monkeypatch.setattr(repository, 'root_id', mock_root_id)
    response = client.post('/folder', json={'name': 'child'})
    assert response.status_code == 404 and response.json()['error']['code'] == 'RESOURCE_NOT_FOUND'
    assert session.query(Folder).count() == before



