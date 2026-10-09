"""Assistant HTTP contracts using actual SQL, with explicit stream engine seam."""
from types import SimpleNamespace
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.api.dependencies import get_current_user,get_current_workspace
from app.exceptions import register_exception_handlers
from app.infra.extensions import get_db, get_session_factory
from sqlalchemy.orm import sessionmaker
from app.features.assistant import router as assistant
from app.features.chat import router as chat
from app.models.assistant import AssistantRun

@pytest.fixture
def contract(session,test_user,test_workspace):
    app=FastAPI();register_exception_handlers(app)
    app.include_router(assistant.router);app.include_router(chat.router)
    app.dependency_overrides[get_db]=lambda:session
    app.dependency_overrides[get_session_factory]=lambda:sessionmaker(bind=session.get_bind(), join_transaction_mode="create_savepoint")
    app.dependency_overrides[get_current_user]=lambda:test_user
    app.dependency_overrides[get_current_workspace]=lambda:test_workspace
    return TestClient(app,raise_server_exceptions=False),session,test_user,test_workspace

def data(response,status=200):
    assert response.status_code==status,response.text
    body=response.json();assert set(body)=={'success','data'} and body['success'] is True
    return body['data']

def create(client,mode='fast'):
    return data(client.post('/assistant/conversations',json={'mode':mode,'title':'Contract'}),201)

def test_conversation_crud_and_empty_run(contract):
    client,db,user,workspace=contract
    row=create(client);cid=row['id']
    assert data(client.get('/assistant/conversations'))['conversations']==[row]
    assert data(client.get(f'/assistant/conversations/{cid}/messages'))=={'conversation':row,'messages':[]}
    assert data(client.get(f'/assistant/conversations/{cid}/active-run')) is None
    assert data(client.get('/assistant/runs/active')) is None
    updated=data(client.patch(f'/assistant/conversations/{cid}',json={'title':'Archived','status':'archived'}))
    assert updated['title']=='Archived' and updated['status']=='archived'
    assert data(client.get('/assistant/conversations'))['conversations']==[]
    deleted=client.delete(f'/assistant/conversations/{cid}')
    assert deleted.status_code==204 and deleted.content==b''

def test_run_messages_cancel_handoff(contract,monkeypatch):
    client,db,user,workspace=contract
    cid=create(client)['id']
    _,run,_=assistant.service.prepare_run(db,workspace,user.id,cid,'Question')
    payload=data(client.get(f'/assistant/runs/{run.id}'))
    assert payload['status']=='running' and payload['pending_permission'] is None
    assert data(client.get(f'/assistant/conversations/{cid}/active-run'))['id']==run.id
    messages=data(client.get(f'/assistant/conversations/{cid}/messages'))['messages']
    assert [m['role'] for m in messages]==['user','assistant']
    assert messages[0]['content']=='Question' and messages[0]['metadata']=={}
    notified=[]
    async def cancel(run_id):notified.append(run_id)
    monkeypatch.setattr(assistant.service.run_registry,'request_cancel',cancel)
    assert data(client.post(f'/assistant/runs/{run.id}/cancel'))['cancel_requested'] is True
    assert notified==[run.id]
    handed=data(client.post(f'/assistant/conversations/{cid}/handoff',json={}),201)
    assert handed['mode']=='expert' and handed['source_conversation_id']==cid

def test_permission_once_response(contract):
    client,db,user,workspace=contract
    cid=create(client,'expert')['id']
    _,run,_=assistant.service.prepare_run(db,workspace,user.id,cid,'Operate')
    run.status='waiting_permission';run.pending_permission_id='p1'
    run.metadata_dict={'pending_permission':{'permission_id':'p1','title':'Read file'}}
    db.commit()
    waiting=data(client.get(f'/assistant/runs/{run.id}'))
    assert waiting['pending_permission']=={'permission_id':'p1','title':'Read file','tool':None}
    assert data(client.get('/assistant/runs/active'))['id']==run.id
    replied=data(client.post(f'/assistant/runs/{run.id}/permissions/p1',json={'response':'once'}))
    assert replied['pending_permission'] is None
    db.expire_all()
    assert db.get(AssistantRun,run.id).permission_response=='once'
    assert client.post(f'/assistant/runs/{run.id}/permissions/p1',json={'response':'once'}).status_code==409

def test_private_field_filter_and_invalid_output(contract,monkeypatch):
    client,db,user,workspace=contract
    expected=create(client)
    monkeypatch.setattr(assistant.service,'create_conversation',lambda *a:SimpleNamespace(to_dict=lambda:{**expected,'credential':'private-secret'}))
    assert data(client.post('/assistant/conversations',json={}),201)==expected
    monkeypatch.setattr(assistant.service,'create_conversation',lambda *a:SimpleNamespace(to_dict=lambda:{'id':'private-secret'}))
    response=client.post('/assistant/conversations',json={})
    assert response.status_code==500 and response.json()['error']['code']=='INTERNAL_ERROR'
    assert 'private-secret' not in response.text

def test_sse_preserves_events_and_headers(contract,monkeypatch):
    from app.features.assistant.event_protocol import AssistantEvent
    client,db,user,workspace=contract
    cid=create(client)['id']
    async def events(*args, **kwargs):
        yield AssistantEvent(type='text_delta',payload={'delta':'hello'})
        yield AssistantEvent(type='done',payload={})
    monkeypatch.setattr(assistant.service,'stream_run',events)
    response=client.post(f'/assistant/conversations/{cid}/messages/stream',json={'query':'Question'})
    assert response.status_code==200 and response.headers['content-type'].startswith('text/event-stream')
    assert 'data: ' in response.text and 'hello' in response.text and '"success"' not in response.text
    assert int(response.headers['x-assistant-run-id'])>0 and response.headers['x-accel-buffering']=='no'

def test_openapi_json_stream_and_no_body(contract):
    client,*_=contract
    schema=client.get('/openapi.json').json()
    for path,ops in schema['paths'].items():
        for op in ops.values():
            responses=op['responses']
            if '204' in responses:assert 'content' not in responses['204']
            elif path.endswith('/stream') or path=='/chat':assert set(responses['200']['content'])=={'text/event-stream'}
            else:
                code='201' if '201' in responses else '200'
                ref=responses[code]['content']['application/json']['schema']['$ref']
                assert schema['components']['schemas'][ref.split('/')[-1]]['properties']['success']['const'] is True


def test_main_api_openapi_requires_typed_json_success_for_every_public_route():
    from app.api.factory import create_fastapi_app
    schema=create_fastapi_app().openapi()
    audited=0
    for path,ops in schema['paths'].items():
        if not path.startswith('/api/'):continue
        for op in ops.values():
            for code,response in op['responses'].items():
                if not code.startswith('2'):continue
                if code=='204':
                    assert 'content' not in response,(path,code)
                    continue
                content=response.get('content',{})
                if 'application/json' not in content:
                    assert path in {'/api/chat','/api/share/{token}','/api/files/{id}/download'} or path.endswith('/messages/stream'),path
                    continue
                ref=content['application/json']['schema']['$ref']
                model=schema['components']['schemas'][ref.split('/')[-1]]
                assert model['properties']['success']['const'] is True,path
                assert 'data' in model['required'],path
                audited+=1
    assert audited>=60
