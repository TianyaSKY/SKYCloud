"""HTTP validation, filtering and binary contracts for inbox/share/settings."""
from io import BytesIO
from unittest.mock import Mock
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.api.dependencies import get_current_user, require_admin
from app.exceptions import register_exception_handlers
from app.infra.extensions import get_db, get_session_factory
from sqlalchemy.orm import sessionmaker
from app.features.inbox import router as inbox
from app.features.share import router as share
from app.features.sys_dict import router as settings
from app.models.inbox import Inbox
from app.models.file import File

@pytest.fixture
def client(session, test_user):
    app=FastAPI(); register_exception_handlers(app)
    for router in (inbox.router,share.router,settings.router): app.include_router(router)
    app.dependency_overrides[get_db]=lambda:session
    app.dependency_overrides[get_session_factory]=lambda:sessionmaker(bind=session.get_bind(), join_transaction_mode="create_savepoint")
    app.dependency_overrides[get_current_user]=lambda:test_user
    app.dependency_overrides[require_admin]=lambda:test_user
    return TestClient(app,raise_server_exceptions=False)

def data(response,status=200):
    assert response.status_code==status,response.text
    payload=response.json()
    assert set(payload)=={'success','data'} and payload['success'] is True
    return payload['data']

def test_inbox_real_sql_page_read_and_no_content_delete(client,session,test_user):
    msg=Inbox(user_id=test_user.id,content='notice',title=None)
    session.add(msg);session.commit()
    listing=data(client.get('/inbox'))
    assert listing['total']==1 and listing['current_page']==1 and listing['pages']==1
    assert listing['items'][0]['title'] is None
    assert data(client.put(f'/inbox/{msg.id}/read'))['is_read'] is True
    assert data(client.put('/inbox/read-all'))=={'message':'All messages marked as read'}
    deleted=client.delete(f'/inbox/{msg.id}')
    assert deleted.status_code==204 and deleted.content==b''
    assert data(client.get('/inbox'))['items']==[]

def test_share_real_sql_creation_listing_cancel_and_stream(client,session,test_user,test_workspace,monkeypatch):
    row=File(name='你好.txt',file_path='private-key',file_size=2,workspace_id=test_workspace.id,uploader_id=test_user.id)
    session.add(row);session.commit()
    created=data(client.post('/share',json={'file_id':row.id}),201)
    assert created['file_name']=='你好.txt' and created['expires_at'] is None
    assert data(client.get('/share/my'))[0]==created
    storage=Mock();stream=BytesIO(b'ab');storage.get_file_stream.return_value=stream
    monkeypatch.setattr(share,'get_storage_client',lambda:storage)
    monkeypatch.setattr(share.share_service,'get_storage_client',lambda:storage)
    downloaded=client.get(created['link'].removeprefix('/api'))
    assert downloaded.content==b'ab' and downloaded.status_code==200
    assert "%E4%BD%A0%E5%A5%BD.txt" in downloaded.headers['content-disposition']
    storage.close_file_stream.assert_called_once_with(stream)
    assert data(client.delete(f"/share/{created['id']}"))=={'message':'Share cancelled successfully'}
    assert client.get(created['link'].removeprefix('/api')).status_code==404

def test_settings_real_sql_crud_and_database_lengths(client,session,test_user,monkeypatch):
    test_user.role="admin";session.commit()
    monkeypatch.setattr(settings.sys_dict_service,'_invalidate_sys_dict_cache',lambda:None)
    # Read the actual DB rather than a shared Redis cache in this SQL fixture.
    def records(session):return settings.sys_dict_service._get_sys_dict_all_cached.__wrapped__(session)
    monkeypatch.setattr(settings.sys_dict_service,'get_sys_dict_all_sync',records)
    body={'key':'k'*100,'value':'v'*255,'des':'d'*2048}
    created=data(client.post('/sys_dicts',json=body),201)
    assert data(client.get(f"/sys_dicts/{created['id']}"))==created
    assert data(client.get('/sys_dicts'))==[created]
    assert data(client.put(f"/sys_dicts/{created['id']}",json={**body,'enable':False}))['enable'] is False
    for field,value in [('key','k'*101),('value','v'*256),('des','d'*2049)]:
        assert client.post('/sys_dicts',json={**body,field:value}).status_code==422
    deleted=client.delete(f"/sys_dicts/{created['id']}")
    assert deleted.status_code==204 and deleted.content==b''

@pytest.mark.parametrize('path,service,method',[('/share/my',share.share_service,'get_my_shares'),('/inbox',inbox.inbox_service,'get_user_inbox')])
def test_malformed_success_fails_without_private_data(client,monkeypatch,path,service,method):
    value=[{'id':'private-secret'}] if path=='/share/my' else {'items':[],'total':'private-secret','pages':0,'page':1}
    monkeypatch.setattr(service,method,lambda *args:value)
    response=client.get(path)
    assert response.status_code==500 and response.json()['error']['code']=='INTERNAL_ERROR'
    assert 'private-secret' not in response.text

def test_openapi_declares_validated_json_and_excludes_download_and_204(client):
    schema=client.get('/openapi.json').json()
    for path,ops in schema['paths'].items():
        for op in ops.values():
            responses=op['responses']
            if '204' in responses:assert 'content' not in responses['204']
            elif path=='/share/{token}':assert 'application/json' not in responses['200'].get('content',{})
            else:
                code='201' if '201' in responses else '200'
                ref=responses[code]['content']['application/json']['schema']['$ref']
                assert schema['components']['schemas'][ref.split('/')[-1]]['properties']['success']['const'] is True


def test_share_creation_cannot_publish_nonmember_file(client,session,test_workspace):
    from app.models.user import User
    from app.models.workspace import WorkspaceMember
    outsider=User(username='other',password_hash='unused')
    session.add(outsider);session.flush()
    row=File(name='private.txt',file_path='private-key',file_size=2,workspace_id=test_workspace.id,uploader_id=outsider.id)
    session.add(row);session.commit()
    # The authenticated fixture user is removed before attempting to share.
    session.query(WorkspaceMember).filter_by(workspace_id=test_workspace.id).delete()
    session.commit()
    response=client.post('/share',json={'file_id':row.id})
    assert response.status_code==403 and response.json()['error']['code']=='PERMISSION_DENIED'
    from app.models.share import Share
    assert session.query(Share).count()==0

@pytest.mark.parametrize('path,service,method,payload',[
    ('/share/my',share.share_service,'get_my_shares',[{'id':1,'token':'public-link','file_id':2,'file_name':None,'created_at':None,'expires_at':None,'link':'/api/share/public-link','internal_key':'private-secret'}]),
    ('/inbox',inbox.inbox_service,'get_user_inbox',{'items':[],'total':0,'pages':0,'page':1,'internal_key':'private-secret'}),
])
def test_success_filters_unexpected_service_fields(client,monkeypatch,path,service,method,payload):
    monkeypatch.setattr(service,method,lambda *args:payload)
    assert client.get(path).status_code==200
    assert 'private-secret' not in client.get(path).text
