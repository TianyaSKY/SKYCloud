"""Real routes and persistence must share the explicit Session contract."""
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.dependencies import get_current_user
from app.exceptions import register_exception_handlers
from app.features.sys_dict import router as module
from app.infra.extensions import get_db, get_session_factory
from sqlalchemy.orm import sessionmaker
from app.models.sys_dict import SysDict


def test_real_http_dictionary_crud_and_admin(session, admin_user, test_user):
    app = FastAPI()
    app.include_router(module.router)
    register_exception_handlers(app)
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_session_factory] = lambda: sessionmaker(bind=session.get_bind(), join_transaction_mode="create_savepoint")
    app.dependency_overrides[get_current_user] = lambda: admin_user
    with TestClient(app) as client:
        payload = {'key': 'http_dict', 'value': 'one', 'des': 'Description', 'enable': True}
        created = client.post('/sys_dicts', json=payload)
        assert created.status_code == 201, created.text
        identifier = created.json()['data']['id']
        assert client.get(f'/sys_dicts/{identifier}').json()['data']['value'] == 'one'
        assert any(item['id'] == identifier for item in client.get('/sys_dicts').json()['data'])
        payload['value'] = 'two'
        assert client.put(f'/sys_dicts/{identifier}', json=payload).json()['data']['value'] == 'two'
        assert client.get(f'/sys_dicts/{identifier}').json()['data']['value'] == 'two'
        assert client.post('/sys_dicts', json={**payload, 'key': 'chat_api_key'}).status_code == 400
        hidden = SysDict(key='chat_api_key', value='hidden legacy value', des='', enable=True)
        session.add(hidden)
        session.commit()
        hidden_id = hidden.id
        listing = client.get('/sys_dicts').json()['data']
        assert all(item['key'] != 'chat_api_key' for item in listing)
        assert client.get(f'/sys_dicts/{hidden_id}').status_code == 404
        assert client.put(f'/sys_dicts/{hidden_id}', json=payload).status_code == 400
        app.dependency_overrides[get_current_user] = lambda: test_user
        assert client.get('/sys_dicts').status_code == 403
        assert client.post('/sys_dicts', json=payload).status_code == 403
        assert client.delete(f'/sys_dicts/{identifier}').status_code == 403
        app.dependency_overrides[get_current_user] = lambda: admin_user
        assert client.delete(f'/sys_dicts/{identifier}').status_code == 204
        assert client.get(f'/sys_dicts/{identifier}').status_code == 404
        assert all(item['id'] != identifier for item in client.get('/sys_dicts').json()['data'])
