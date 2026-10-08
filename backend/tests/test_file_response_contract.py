"""HTTP contracts for file/folder JSON, split uploads, and untouched download bytes."""
from io import BytesIO
from unittest.mock import AsyncMock, Mock
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.api.dependencies import get_current_user, get_current_workspace
from app.exceptions import register_exception_handlers
from app.features.file import router as files
from app.features.folder import router as folders
from app.infra.extensions import get_db
from app.models.file import File
from app.models.folder import Folder

@pytest.fixture
def contract(session, test_user, test_workspace):
    root = session.query(Folder).filter_by(workspace_id=test_workspace.id, parent_id=None).one()
    row = File(name='你好.txt', file_path='private-object-key', file_size=2,
               workspace_id=test_workspace.id, uploader_id=test_user.id, parent_id=root.id)
    session.add(row); session.commit()
    app = FastAPI(); register_exception_handlers(app)
    app.include_router(files.router); app.include_router(folders.router)
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_user] = lambda: test_user
    app.dependency_overrides[get_current_workspace] = lambda: test_workspace
    return TestClient(app, raise_server_exceptions=False), row, root


def data(response, status=200):
    assert response.status_code == status, response.text
    payload = response.json()
    assert set(payload) == {'success', 'data'} and payload['success'] is True
    return payload['data']


def test_directory_search_metadata_and_processing_use_typed_envelopes(contract):
    client, row, root = contract
    assert data(client.get(f'/files/{row.id}'))['id'] == row.id
    listing = data(client.get('/files/list', params={'parent_id':root.id}))
    assert listing['files']['items'][0]['id'] == row.id
    assert listing['files']['total'] == 1
    assert data(client.get('/files/search', params={'q':'你好','type':'fuzzy'}))['total'] == 1
    assert data(client.get('/files/search'))['items'] == []
    assert data(client.get('/files/process_status')) == {'处理中':1,'成功':0,'失败':0}
    assert data(client.get('/folder/root_id')) == {'root_folder_id':root.id}
    assert data(client.get(f'/folder/{root.id}'))['path'] == '/'


def test_folder_length_crud_matches_database_column(contract):
    client, row, root = contract
    created = data(client.post('/folder', json={'name':'x'*128}), 201)
    assert created['name'] == 'x'*128 and created['parent_id'] == root.id
    assert client.post('/folder', json={'name':'x'*129}).status_code == 422
    assert client.put(f"/folder/{created['id']}", json={'name':'x'*129}).status_code == 422
    assert data(client.put(f"/folder/{created['id']}", json={'name':'renamed'}))['name'] == 'renamed'
    assert any(f['id'] == created['id'] for f in data(client.get('/folder/all'))['folders'])
    deleted = client.delete(f"/folder/{created['id']}")
    assert deleted.status_code == 204 and deleted.content == b''


@pytest.mark.parametrize('path', ['/files','/files/batch','/files/multipart/complete'])
def test_upload_created_metadata_is_validated_filtered_and_201(contract, monkeypatch, path):
    client, row, root = contract
    expected = row.to_dict()
    monkeypatch.setattr(row, 'to_dict', lambda: {**expected,'file_path':'private-object-key','vector_info':[1,2]})
    if path == '/files':
        monkeypatch.setattr(files.file_service, 'create_uploaded_file', lambda *args: row)
        response = client.post(path, files={'file':('hello.txt',b'ab')})
    elif path == '/files/batch':
        monkeypatch.setattr(files.file_service, 'create_uploaded_files', lambda *args: [row])
        response = client.post(path, files={'files':('hello.txt',b'ab')})
    else:
        monkeypatch.setattr(files.file_service, 'complete_multipart_upload', lambda *args: row)
        response = client.post(path,json={'upload_id':'owned-upload'})
    assert data(response,201) == ([expected] if path == '/files/batch' else expected)
    assert 'private-object-key' not in response.text and 'vector_info' not in response.text


@pytest.mark.parametrize('instant', [False,True])
def test_preflight_and_init_discriminate_miss_and_committed_reuse(contract, monkeypatch, instant):
    client, row, root = contract
    response = dict(instant_upload=instant, exists=instant)
    init = dict(instant_upload=instant,upload_id=None if instant else 'owned-upload',
                chunk_size=4,total_chunks=1,uploaded_chunks=[])
    if instant:
        response['file'] = row.to_dict(); init['file'] = row.to_dict()
    monkeypatch.setattr(files.file_service, 'preflight_file_upload', lambda *args: response)
    monkeypatch.setattr(files.file_service, 'init_multipart_upload', lambda *args: init)
    body = dict(filename='hello.txt',total_size=2,content_hash='a'*64)
    assert data(client.post('/files/preflight',json=body)) == response
    assert data(client.post('/files/multipart/init',json=body)) == init


def test_instant_upload_without_committed_file_is_safe_response_error(contract, monkeypatch):
    client, row, root = contract
    monkeypatch.setattr(files.file_service, 'preflight_file_upload', lambda *args: {'instant_upload':True,'exists':True})
    response=client.post('/files/preflight',json=dict(filename='hello.txt',total_size=2,content_hash='a'*64))
    assert response.status_code == 500 and response.json()['error']['code'] == 'INTERNAL_ERROR'


def test_multipart_status_and_chunk_ack_filter_storage_details(contract, monkeypatch):
    client, row, root = contract
    status = dict(upload_id='owned-upload',chunk_size=4,total_chunks=2,uploaded_chunks=[0])
    chunk = dict(upload_id='owned-upload',chunk_index=0,uploaded_chunks=[0])
    monkeypatch.setattr(files.file_service,'get_multipart_upload_status',lambda *args:{**status,'object_name':'private-key'})
    monkeypatch.setattr(files.file_service,'save_multipart_chunk',lambda *args:{**chunk,'stage_id':'private-stage'})
    assert data(client.get('/files/multipart/owned-upload')) == status
    assert data(client.post('/files/multipart/chunk',data={'upload_id':'owned-upload','chunk_index':0},files={'chunk':('part',b'ab')})) == chunk


def test_search_failure_is_canonical_error_and_not_empty_success(contract, monkeypatch):
    client, row, root = contract
    monkeypatch.setattr(files.file_service,'search_files',AsyncMock(return_value={'items':[],'total':0,'error':'private-provider-details'}))
    response=client.get('/files/search',params={'q':'query','type':'semantic'})
    assert response.status_code == 500
    assert response.json() == {'success':False,'error':{'code':'INTERNAL_ERROR','message':'Internal server error'}}
    assert 'private' not in response.text


def test_download_preserves_bytes_disposition_and_stream_cleanup(contract, monkeypatch):
    client, row, root = contract
    stream=BytesIO(b'ab')
    storage=Mock();storage.get_file_stream.return_value=stream
    monkeypatch.setattr(files,'get_storage_client',lambda:storage)
    monkeypatch.setattr(files.file_service,'get_downloadable_file',lambda *args:row)
    response=client.get(f'/files/{row.id}/download')
    assert response.status_code == 200 and response.content == b'ab'
    assert "filename*=UTF-8''%E4%BD%A0%E5%A5%BD.txt" in response.headers['content-disposition']
    storage.close_file_stream.assert_called_once_with(stream)


def test_openapi_documents_json_envelopes_binary_and_no_content(contract):
    client, row, root = contract
    schema=client.get('/openapi.json').json()
    for path,operations in schema['paths'].items():
        for operation in operations.values():
            responses=operation['responses']
            if '204' in responses:
                assert 'content' not in responses['204']
            elif path.endswith('/download'):
                assert 'application/json' not in responses['200'].get('content',{})
            else:
                code='201' if '201' in responses else '200'
                ref=responses[code]['content']['application/json']['schema']['$ref']
                assert schema['components']['schemas'][ref.split('/')[-1]]['properties']['success']['const'] is True


def test_missing_root_cannot_create_a_second_root(contract, monkeypatch):
    from app.features.folder import repository
    client, row, root = contract
    session = row._sa_instance_state.session
    before = session.query(Folder).count()
    monkeypatch.setattr(repository,'root_id',lambda *args:None)
    response=client.post('/folder',json={'name':'child'})
    assert response.status_code == 404 and response.json()['error']['code'] == 'RESOURCE_NOT_FOUND'
    assert session.query(Folder).count() == before


def test_avatar_filters_private_metadata_and_preserves_public_alias(contract, monkeypatch):
    client, row, root = contract
    expected={**row.to_dict(),'avatar_url':'/api/share/issued-avatar','avatar':'/api/share/issued-avatar'}
    monkeypatch.setattr(files.file_service,'upload_avatar_for_user',lambda *args:{**row.to_dict(),'avatar_url':'/api/share/issued-avatar','file_path':'private-key'})
    response=client.post('/files/upload/avatar/1',files={'avatar':('avatar.png',b'ab','image/png')})
    assert data(response) == expected and 'private-key' not in response.text
