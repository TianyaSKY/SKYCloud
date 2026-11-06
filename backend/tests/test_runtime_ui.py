"""主机绑定运行时 UI 凭据的 HTTP 安全性与生命周期测试."""
from collections.abc import AsyncGenerator, Generator
import json
from typing import Any, cast
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qs

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from app.features.auth.refresh_sessions import AccessClaims
from app.features.auth.service import generate_token
from app.features.workspace import ui_tickets
from app.features.workspace.router import router
from app.features.workspace.ui_gateway import router as gateway
from app.infra.extensions import get_db, get_session_factory
from app.models.opencode_runtime import OpenCodeRuntime
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember


class CredentialStore:
    def __init__(self) -> None:
        self.entries: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    def set(self, key: str, value: str, ex: int, nx: bool) -> bool:
        assert nx and ex > 0
        if key in self.entries:
            return False
        self.entries[key] = value
        self.ttls[key] = ex
        return True

    def get(self, key: str) -> str | None:
        return self.entries.get(key)

    def getdel(self, key: str) -> str | None:
        return self.entries.pop(key, None)


@pytest.fixture
def ui(
    session: Session, test_user: User, test_workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> Generator[tuple[TestClient, CredentialStore, OpenCodeRuntime], None, None]:
    monkeypatch.setenv('APP_ORIGIN', 'http://app.skycloud.localhost')
    monkeypatch.setenv('RUNTIME_UI_ORIGIN_TEMPLATE', 'http://r{runtime_id}.skycloud.localhost:8081')
    runtime = OpenCodeRuntime(workspace_id=test_workspace.id, user_id=test_user.id,
                             status='running', container_id='a' * 64)
    session.add(runtime)
    session.commit()
    store = CredentialStore()
    monkeypatch.setattr(ui_tickets, 'redis_client', store)
    app = FastAPI()
    app.include_router(router, prefix='/api')
    app.include_router(gateway)
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_session_factory] = lambda: sessionmaker(bind=session.get_bind(), join_transaction_mode="create_savepoint")
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, store, runtime


def issue(ui: tuple[TestClient, CredentialStore, OpenCodeRuntime], user: User, workspace: Workspace) -> tuple[str, str]:
    client, _, _ = ui
    auth_token = generate_token(user.id, user.auth_version or 0)
    assert auth_token is not None
    response = client.post(
        f'/api/workspace/{workspace.id}/opencode/ui-ticket',
        headers={'Authorization': 'Bearer ' + auth_token},
    )
    assert response.status_code == 200, response.text
    assert response.headers['cache-control'] == 'no-store'
    launch_url = str(response.json()['data']['launch_url'])
    url = urlsplit(launch_url)
    ticket = parse_qs(url.query)['ticket'][0]
    return url.netloc, ticket


def launch(ui: tuple[TestClient, CredentialStore, OpenCodeRuntime], user: User, workspace: Workspace) -> tuple[str, str, str]:
    client, _, _ = ui
    host, ticket = issue(ui, user, workspace)
    response = client.get('/runtime-ui/__skycloud/launch?ticket=' + ticket,
                          headers={'Host': host}, follow_redirects=False)
    assert response.status_code == 303, response.text
    assert response.headers['location'] == '/'
    assert 'HttpOnly' in response.headers['set-cookie']
    assert 'SameSite=strict' in response.headers['set-cookie']
    raw = response.cookies[ui_tickets.COOKIE]
    return host, raw, ticket


def test_ticket_one_use_and_hash_only_store(
    ui: tuple[TestClient, CredentialStore, OpenCodeRuntime], test_user: User, test_workspace: Workspace
) -> None:
    host, raw, ticket = launch(ui, test_user, test_workspace)
    client, store, _ = ui
    assert client.get('/runtime-ui/__skycloud/launch?ticket=' + ticket,
                      headers={'Host': host}, follow_redirects=False).status_code == 401
    assert len(store.entries) == 1
    key = next(iter(store.entries))
    assert raw not in key and ticket not in key
    assert json.loads(store.entries[key])['user_id'] == test_user.id
    assert set(store.ttls.values()) == {30, 600}


def test_renew_rotates_cookie_without_exposing_session_or_loading_runtime(
    ui: tuple[TestClient, CredentialStore, OpenCodeRuntime], test_user: User, test_workspace: Workspace
) -> None:
    host, previous, _ = launch(ui, test_user, test_workspace)
    host, ticket = issue(ui, test_user, test_workspace)
    client, store, _ = ui
    response = client.get('/runtime-ui/__skycloud/renew?ticket=' + ticket, headers={'Host': host})
    assert response.status_code == 200
    raw = response.cookies[ui_tickets.COOKIE]
    assert raw != previous and raw not in response.text
    assert 'HttpOnly' in response.headers['set-cookie'] and 'SameSite=strict' in response.headers['set-cookie']
    assert 'skycloud-runtime-ui-renewed' in response.text and ticket in response.text
    assert 'http://app.skycloud.localhost' in response.text
    assert "default-src 'none'" in response.headers['content-security-policy']
    assert response.headers['cache-control'] == 'no-store'
    assert client.get('/runtime-ui/__skycloud/renew?ticket=' + ticket, headers={'Host': host}).status_code == 401
    # 现存流保留其有界的旧租约; 新的 HTTP/WS 请求使用
    # 轮转后的主机 Cookie. 每次租约仍检查权威身份.
    assert len(store.entries) == 2


def test_renew_rechecks_permission_after_ticket_issuance(
    ui: tuple[TestClient, CredentialStore, OpenCodeRuntime], session: Session, test_user: User, test_workspace: Workspace
) -> None:
    host, ticket = issue(ui, test_user, test_workspace)
    session.execute(update(WorkspaceMember).where(WorkspaceMember.workspace_id == test_workspace.id,
                    WorkspaceMember.user_id == test_user.id).values(role='viewer'))
    session.commit()
    response = ui[0].get('/runtime-ui/__skycloud/renew?ticket=' + ticket, headers={'Host': host})
    assert response.status_code == 403 and 'set-cookie' not in response.headers


def test_parent_login_logout_immediately_revokes_runtime_ui_cookie(
    ui: tuple[TestClient, CredentialStore, OpenCodeRuntime], session: Session, test_user: User, test_workspace: Workspace
) -> None:
    from app.features.auth import refresh_sessions
    import jwt
    from app.infra.extensions import SECRET_KEY
    token = generate_token(test_user.id, test_user.auth_version or 0)
    assert token is not None
    claims = jwt.decode(token, SECRET_KEY, algorithms=['HS256'])
    refresh, access = refresh_sessions.create(session, cast(AccessClaims, claims))
    client, _, _ = ui
    issued = client.post(
        f'/api/workspace/{test_workspace.id}/opencode/ui-ticket',
        headers={'Authorization': 'Bearer ' + access['token']},
    )
    assert issued.status_code == 200
    launch_url = str(issued.json()['data']['launch_url'])
    url = urlsplit(launch_url)
    launch_res = client.get('/runtime-ui/__skycloud/launch?' + url.query,
                        headers={'Host': url.netloc}, follow_redirects=False)
    raw = launch_res.cookies[ui_tickets.COOKIE]
    assert ui_tickets.authenticate(session, raw, url.netloc)
    refresh_sessions.revoke(refresh)
    with pytest.raises(HTTPException) as exc:
        ui_tickets.authenticate(session, raw, url.netloc)
    assert exc.value.status_code == 401


def test_parent_and_runtime_cannot_share_cookie_host_on_different_ports(
    ui: tuple[TestClient, CredentialStore, OpenCodeRuntime], test_user: User, test_workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = ui[2]
    monkeypatch.setenv('APP_ORIGIN', f'http://r{runtime.id}.skycloud.localhost:80')
    auth_token = generate_token(test_user.id)
    assert auth_token is not None
    response = ui[0].post(
        f'/api/workspace/{test_workspace.id}/opencode/ui-ticket',
        headers={'Authorization': 'Bearer ' + auth_token},
    )
    assert response.status_code == 503


@pytest.mark.parametrize('change', ['password', 'demote', 'remove', 'container', 'stop'])
def test_current_identity_revokes_ui_session(
    ui: tuple[TestClient, CredentialStore, OpenCodeRuntime], session: Session, test_user: User, test_workspace: Workspace, change: str
) -> None:
    host, raw, _ = launch(ui, test_user, test_workspace)
    client, _, runtime = ui
    if change == 'password':
        session.execute(update(User).where(User.id == test_user.id).values(auth_version=1))
    elif change == 'demote':
        session.execute(update(WorkspaceMember).where(WorkspaceMember.workspace_id == test_workspace.id,
            WorkspaceMember.user_id == test_user.id).values(role='viewer'))
    elif change == 'remove':
        session.query(WorkspaceMember).filter_by(workspace_id=test_workspace.id, user_id=test_user.id).delete()
    elif change == 'container':
        runtime.container_id = 'b' * 64
    else:
        runtime.status = 'stopped'
    session.commit()
    response = client.get('/runtime-ui/', headers={'Host': host, 'Cookie': ui_tickets.COOKIE + '=' + raw})
    assert response.status_code in {401, 403, 409}, response.text


def test_ui_cookies_cannot_authenticate_rest_and_bearer_cannot_authenticate_ui(
    ui: tuple[TestClient, CredentialStore, OpenCodeRuntime], test_user: User, test_workspace: Workspace
) -> None:
    host, raw, _ = launch(ui, test_user, test_workspace)
    client, _, _ = ui
    assert client.get('/api/workspace', headers={'Cookie': ui_tickets.COOKIE + '=' + raw}).status_code == 401
    client.cookies.clear()
    auth_token = generate_token(test_user.id, test_user.auth_version or 0)
    assert auth_token is not None
    assert client.get('/runtime-ui/', headers={'Host': host, 'Authorization': 'Bearer ' + auth_token}).status_code == 401
    assert client.get('/runtime-ui/', headers={'Host': 'r999.skycloud.localhost:8081',
        'Cookie': ui_tickets.COOKIE + '=' + raw}).status_code == 403


def test_origin_checks_and_bounded_authenticated_proxy(
    ui: tuple[TestClient, CredentialStore, OpenCodeRuntime], test_user: User, test_workspace: Workspace
) -> None:
    host, raw, _ = launch(ui, test_user, test_workspace)
    client, _, _ = ui
    base = {'Host': host, 'Cookie': ui_tickets.COOKIE + '=' + raw}
    assert client.post('/runtime-ui/session', headers=base, json={}).status_code == 403
    assert client.get('/runtime-ui/', headers={**base, 'Origin': 'http://r999.skycloud.localhost:8081'}).status_code == 403
    assert client.post('/runtime-ui/session', headers={**base, 'Origin': 'http://' + host},
                       content=b'x' * (256 * 1024 + 1)).status_code == 413
    sent: list[httpx.Request] = []
    real_client = httpx.AsyncClient

    def transport(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, text='<html>UI</html>', headers={'Content-Type': 'text/html', 'Set-Cookie': 'evil=1'})

    def mock_async_client(**kw: Any) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(transport), **kw)

    with patch('app.features.workspace.ui_gateway.httpx.AsyncClient', side_effect=mock_async_client):
        response = client.get('/runtime-ui/assets/app.js', headers={**base,
            'Authorization': 'Bearer secret', 'X-Runtime-Controller-Token': 'attacker'})
    assert response.status_code == 200
    assert 'set-cookie' not in response.headers
    assert response.headers['referrer-policy'] == 'no-referrer'
    assert sent[0].url.path == '/runtimes/' + 'a' * 64 + '/ui/assets/app.js'
    assert 'authorization' not in sent[0].headers and 'cookie' not in sent[0].headers
    assert sent[0].headers['x-runtime-controller-token'] != 'attacker'


def test_store_expiry_outage_and_bad_configuration_fail_closed(
    ui: tuple[TestClient, CredentialStore, OpenCodeRuntime], test_user: User, test_workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    host, raw, _ = launch(ui, test_user, test_workspace)
    client, store, runtime = ui
    store.entries.clear()
    assert client.get('/runtime-ui/', headers={'Host': host, 'Cookie': ui_tickets.COOKIE + '=' + raw}).status_code == 401
    monkeypatch.setenv('RUNTIME_UI_ORIGIN_TEMPLATE', 'http://localhost:{runtime_id}')
    with pytest.raises(HTTPException) as exc:
        ui_tickets.ui_origin(runtime.id)
    assert exc.value.status_code == 503
    monkeypatch.setenv('RUNTIME_UI_ORIGIN_TEMPLATE', 'http://r{runtime_id}.skycloud.localhost:8081')
    auth_token = generate_token(test_user.id, test_user.auth_version or 0)
    assert auth_token is not None
    with patch.object(store, 'set', side_effect=RuntimeError('private redis password')):
        response = client.post(
            f'/api/workspace/{test_workspace.id}/opencode/ui-ticket',
            headers={'Authorization': 'Bearer ' + auth_token},
        )
    assert response.status_code == 503
    assert 'private redis' not in response.text


@pytest.mark.parametrize('end', ['revoke', 'expire'])
def test_live_stream_ends_on_revocation_or_session_expiry(
    ui: tuple[TestClient, CredentialStore, OpenCodeRuntime], test_user: User, test_workspace: Workspace, monkeypatch: pytest.MonkeyPatch, end: str
) -> None:
    import asyncio
    import time
    from fastapi import HTTPException
    from app.features.workspace import ui_gateway
    host, raw, _ = launch(ui, test_user, test_workspace)
    client, store, _ = ui
    monkeypatch.setattr(ui_gateway, 'RECHECK_SECONDS', 0.01)
    if end == 'expire':
        key = ui_tickets._key('session', raw)  # pyright: ignore[reportPrivateUsage]
        payload = json.loads(store.entries[key])
        payload['expires_at'] = time.time() + 0.07
        store.entries[key] = json.dumps(payload)

    class SlowStream(httpx.AsyncByteStream):
        closed: bool = False
        delivered: bool = False

        async def __aiter__(self) -> AsyncGenerator[bytes, None]:
            yield b'data: first\n\n'
            self.delivered = True
            await asyncio.sleep(0.15)
            yield b'data: forbidden-late\n\n'

        async def aclose(self) -> None:
            self.closed = True

    stream = SlowStream()
    real_client = httpx.AsyncClient

    def transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream, headers={'Content-Type': 'text/event-stream'})

    def recheck(raw: str, host: str, session_factory: Any = None) -> dict[str, Any]:
        if end == 'revoke' and stream.delivered:
            raise HTTPException(401, 'revoked')
        return {}

    def mock_stream_client(**kw: Any) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(transport), **kw)

    with patch('app.features.workspace.ui_gateway.httpx.AsyncClient', side_effect=mock_stream_client), \
         patch.object(ui_gateway, 'reauthenticate', side_effect=recheck):
        response = client.get('/runtime-ui/event', headers={'Host': host,
                        'Cookie': ui_tickets.COOKIE + '=' + raw})
    assert response.status_code == 200
    assert b'first' in response.content and b'forbidden-late' not in response.content
    assert stream.closed


def test_https_launch_uses_secure_host_cookie(
    ui: tuple[TestClient, CredentialStore, OpenCodeRuntime], test_user: User, test_workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv('RUNTIME_UI_ORIGIN_TEMPLATE', 'https://r{runtime_id}.runtime.example.com')
    host, ticket = issue(ui, test_user, test_workspace)
    response = ui[0].get('/runtime-ui/__skycloud/launch?ticket=' + ticket,
                         headers={'Host': host}, follow_redirects=False)
    assert response.status_code == 303
    cookie = response.headers['set-cookie']
    assert cookie.startswith('__Host-' + ui_tickets.COOKIE + '=')
    assert 'Secure' in cookie and 'HttpOnly' in cookie and 'Path=/' in cookie
    assert 'Domain=' not in cookie


def test_mutation_revalidates_before_forwarding(
    ui: tuple[TestClient, CredentialStore, OpenCodeRuntime], session: Session, test_user: User, test_workspace: Workspace
) -> None:
    from app.features.workspace import ui_gateway
    from fastapi import HTTPException
    host, raw, _ = launch(ui, test_user, test_workspace)
    headers = {'Host': host, 'Origin': 'http://' + host, 'Cookie': ui_tickets.COOKIE + '=' + raw}
    with patch.object(ui_gateway, 'reauthenticate', side_effect=HTTPException(401, 'revoked')):
        response = ui[0].post('/runtime-ui/session', headers=headers, json={'title': 'new'})
    assert response.status_code == 401
    real_client = httpx.AsyncClient
    sent: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={'id': 'session-id'})

    def mock_reauth(raw: str, host: str, session_factory: Any = None) -> Any:
        return ui_tickets.authenticate(session, raw, host)

    def mock_mutation_client(**kw: Any) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(transport), **kw)

    with patch.object(ui_gateway, 'reauthenticate', side_effect=mock_reauth), \
         patch('app.features.workspace.ui_gateway.httpx.AsyncClient', side_effect=mock_mutation_client):
        response = ui[0].post('/runtime-ui/session', headers=headers, json={'title': 'new'})
    assert response.status_code == 200
    assert json.loads(sent[0].content) == {'title': 'new'}
