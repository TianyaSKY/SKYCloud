"""Verify FastAPI auth serialization, including filtering and invalid payloads."""
from unittest.mock import AsyncMock
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.api.dependencies import get_current_user, require_admin
from app.exceptions import register_exception_handlers
from app.features.auth import router as auth
from app.infra.extensions import get_db

@pytest.fixture
def client(session, test_user):
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(auth.router)
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_user] = lambda: test_user
    app.dependency_overrides[require_admin] = lambda: test_user
    return TestClient(app, raise_server_exceptions=False)

@pytest.mark.parametrize('path', ['/auth/register', '/users'])
def test_created_users_filter_private_fields(client, test_user, monkeypatch, path):
    expected = test_user.to_dict()
    monkeypatch.setattr(test_user, 'to_dict', lambda: {**expected, 'password_hash': 'private-hash', 'auth_version': 42})
    service, function = (auth.user_service, 'create_user') if path == '/users' else (auth.auth_service, 'register_user')
    monkeypatch.setattr(service, function, lambda *args: test_user)
    response = client.post(path, json={'username': 'new', 'password': 'password'})
    assert response.status_code == 201
    assert 'private-hash' not in response.text and 'auth_version' not in response.text
    assert (response.json()['user'] if path == '/auth/register' else response.json()) == expected

def test_invalid_profile_returns_safe_server_error(client, monkeypatch):
    class InvalidUser:
        def to_dict(self):
            return {'id': 'private-invalid-integer', 'password_hash': 'private-hash'}
    monkeypatch.setattr(auth.user_service, 'get_user', AsyncMock(return_value=InvalidUser()))
    response = client.get('/users/1')
    assert response.status_code == 500
    assert response.json()['error']['code'] == 'INTERNAL_ERROR'
    assert 'private' not in response.text

def test_refresh_preserves_cookie_and_filters_private_fields(client, monkeypatch, test_user):
    payload = dict(token='access-token', user_id=test_user.id, role=test_user.role, expires_in=900,
                   user=dict(id=test_user.id, username=test_user.username, role=test_user.role,
                             avatar=None, password_hash='private-hash'), refresh_token='private-refresh')
    monkeypatch.setattr(auth.refresh_sessions, 'check_origin', lambda *args, **kwargs: None)
    monkeypatch.setattr(auth.refresh_sessions, 'refresh', lambda *args: ('replacement', payload, 600))
    response = client.post('/auth/refresh')
    assert response.status_code == 200 and response.json()['token'] == 'access-token'
    assert 'private' not in response.text
    assert 'HttpOnly' in response.headers['set-cookie']
    assert response.headers['cache-control'] == 'no-store'

def test_mcp_metadata_filters_raw_credential_and_issue_requires_metadata(client, monkeypatch):
    payload = dict(token=None, user_id=1, expires_in_days=365, usage='Bearer', mcp_token='private-credential')
    monkeypatch.setattr(auth.auth_service, 'get_mcp_token', lambda *args: payload)
    response = client.get('/auth/mcp-token')
    assert response.status_code == 200 and 'mcp_token' not in response.json()
    assert response.headers['cache-control'] == 'no-store'
    monkeypatch.setattr(auth.auth_service, 'refresh_mcp_token', lambda *args: payload)
    response = client.post('/auth/mcp-token/refresh')
    assert response.status_code == 500 and 'private-credential' not in response.text

def test_openapi_documents_success_models(client):
    for path, operations in client.get('/openapi.json').json()['paths'].items():
        for method, operation in operations.items():
            if method == 'delete':
                assert 'content' not in operation['responses']['204']
            else:
                code = '201' if path in {'/auth/register', '/users'} else '200'
                assert '$ref' in operation['responses'][code]['content']['application/json']['schema']


def test_explicit_mcp_issue_returns_credential_with_valid_metadata(client, monkeypatch):
    metadata = dict(id=1, user_id=1, name=None, token_preview='sha256:preview',
                    created_at=None, expires_at=None, last_used_at=None, revoked_at=None,
                    is_revoked=False, is_expired=False, token_hash='private-hash')
    payload = dict(token=metadata, user_id=1, expires_in_days=365, usage='Bearer', mcp_token='issued-token')
    monkeypatch.setattr(auth.auth_service, 'refresh_mcp_token', lambda *args: payload)
    response = client.post('/auth/mcp-token/refresh')
    assert response.status_code == 200 and response.json()['mcp_token'] == 'issued-token'
    assert 'private-hash' not in response.text
    assert response.headers['cache-control'] == 'no-store'
