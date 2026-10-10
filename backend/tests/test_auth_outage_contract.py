"""Unavailable identity stores deny access without pretending credentials expired."""
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import jwt
import pytest
from fastapi import Depends, FastAPI
from redis.exceptions import ConnectionError

from app.api import authentication, dependencies
from app.exceptions import register_exception_handlers
from app.features.auth import refresh_sessions, sessions
from app.infra.extensions import SECRET_KEY, get_db, get_session_factory
from app.mcp import token_service


@pytest.mark.parametrize('kind', ['web', 'mcp'])
@pytest.mark.parametrize('stage', ['open', 'validate', 'role', 'profile', 'close'])
async def test_auth_backend_fault_returns_unavailable_without_private_details(monkeypatch, kind, stage):
    private = 'private-database-and-credential-detail'
    def fault(*args): raise RuntimeError(private)
    @contextmanager
    def factory():
        if stage == 'open': fault()
        try:
            yield SimpleNamespace()
        finally:
            if stage == 'close': fault()
    monkeypatch.setattr(sessions, 'valid_session', fault if stage == 'validate' else lambda *args: True)
    monkeypatch.setattr(token_service, 'get_active_mcp_token', fault if stage == 'validate' else lambda *args: True)
    monkeypatch.setattr(authentication.account_repository, 'current_role', fault if stage == 'role' else lambda *args: 'common')
    monkeypatch.setattr(authentication.user_service, '_get_user_data', fault if stage == 'profile' else lambda *args: {'id': 1, 'role': 'common'})
    payload = {'sub': '1', 'ver': 0}
    if kind == 'mcp': payload['type'] = 'mcp'
    app = FastAPI()
    register_exception_handlers(app)
    @app.get('/protected')
    async def protected(user=Depends(dependencies.get_current_user)):
        pytest.fail('Unavailable authentication must not enter the endpoint')
    app.dependency_overrides[get_db] = lambda: None
    app.dependency_overrides[get_session_factory] = lambda: factory
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
        response = await client.get('/protected', headers={'Authorization': 'Bearer ' + jwt.encode(payload, SECRET_KEY, algorithm='HS256')})
    assert response.status_code == 503
    assert response.json()['error']['code'] == 'SERVICE_UNAVAILABLE'
    assert private not in response.text


async def test_browser_family_store_outage_propagates_unavailable(monkeypatch):
    from fastapi import HTTPException
    def unavailable(*args): raise ConnectionError('private-redis-credentials')
    monkeypatch.setattr(refresh_sessions.redis_client, 'get', unavailable)
    payload = {'sub': '1', 'ver': 0, 'sid': 'a' * 43}
    raw = jwt.encode(payload, SECRET_KEY, algorithm='HS256')
    credentials = dependencies.HTTPAuthorizationCredentials(scheme='Bearer', credentials=raw)
    with pytest.raises(HTTPException) as error:
        await dependencies.get_current_user(credentials, Mock(), lambda: nullcontext(Mock()))
    assert error.value.status_code == 503


@pytest.mark.parametrize('sub', ['not-a-user', '0', '-1'])
@pytest.mark.parametrize('kind', ['web', 'mcp'])
async def test_invalid_subject_stays_unauthorized_without_store_access(sub, kind):
    from fastapi import HTTPException
    factory = Mock(side_effect=AssertionError('Malformed identity must not access SQL'))
    payload = {'sub': sub, 'ver': 0}
    if kind == 'mcp': payload['type'] = 'mcp'
    raw = jwt.encode(payload, SECRET_KEY, algorithm='HS256')
    credentials = dependencies.HTTPAuthorizationCredentials(scheme='Bearer', credentials=raw)
    with pytest.raises(HTTPException) as error:
        await dependencies.get_current_user(credentials, Mock(), factory)
    assert error.value.status_code == 401
    factory.assert_not_called()
