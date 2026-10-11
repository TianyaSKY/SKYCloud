"""Real PostgreSQL/Redis/Controller/OpenCode interactive terminal and revocation."""
import os
import json
import time
import uuid
import threading
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import sessionmaker
from starlette.websockets import WebSocketDisconnect

from tests.integration.fixture_paths import FIXTURE_DIR
from tests.integration.test_runtime_security import infrastructure, controller, docker
from tests.integration.test_migrations import postgres
from entry.migrate import upgrade
from app.infra.extensions import get_db, get_session_factory
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember
from app.models.opencode_runtime import OpenCodeRuntime
from app.models.runtime_operation import RuntimeOperation
from app.features.workspace import docker_service, ui_gateway, ui_tickets
from app.features.workspace.router import router
from app.features.auth.service import generate_token

pytestmark = pytest.mark.skipif(os.getenv('RUN_RUNTIME_UI_TESTS') != '1',
                               reason='Opt-in real runtime terminal integration')


@pytest.mark.parametrize('use_ticket', [False, True])
@pytest.mark.parametrize('revocation', ['membership', 'refresh-replay'])
def test_real_terminal_and_current_session_revocation(infrastructure, controller, postgres, monkeypatch, use_ticket, revocation, tmp_path):
    upgrade(postgres)
    factory = sessionmaker(bind=postgres)
    monkeypatch.setattr(ui_tickets, 'redis_client', infrastructure['redis'])
    def global_factory():
        pytest.fail('Gateway must use injected factory for ongoing authorization')
    monkeypatch.setattr(ui_gateway, 'SessionLocal', global_factory)
    monkeypatch.setattr(ui_gateway, 'RECHECK_SECONDS', 0.05)
    monkeypatch.setenv('APP_ORIGIN', 'http://app.skycloud.localhost')
    monkeypatch.setenv('RUNTIME_UI_ORIGIN_TEMPLATE', 'http://r{runtime_id}.skycloud.localhost:8081')
    user_id, ws_id, runtime_id = [int(uuid.uuid4().hex[:7], 16) + 1 for _ in range(3)]
    name = f'skycloud-opencode-w{ws_id}-u{user_id}'
    # Never replace an existing container, including unrelated managed ones.
    assert docker('inspect', name, check=False).returncode != 0
    infrastructure['cleanup'].append(name)
    with factory() as db:
        db.add(User(id=user_id, username='isolated-terminal-owner', password_hash='test-hash', role='common'))
        db.flush()
        db.add(Workspace(id=ws_id, name='Isolated real terminal', owner_id=user_id))
        db.flush()
        db.add(WorkspaceMember(workspace_id=ws_id, user_id=user_id, role='editor'))
        db.add(OpenCodeRuntime(id=runtime_id, workspace_id=ws_id, user_id=user_id, status='stopped'))
        db.commit()
        workspace = db.get(Workspace, ws_id)
        try:
            docker_service.start(db, workspace, user_id)
        finally:
            # Register exact candidates even when production start fails partway.
            with factory() as cleanup_db:
                for generation,candidate in cleanup_db.query(RuntimeOperation.generation,
                        RuntimeOperation.candidate_container_id).filter_by(workspace_id=ws_id,user_id=user_id).all():
                    generation_name=f'skycloud-opencode-w{ws_id}-u{user_id}-g{generation}'
                    for identity in (generation_name,candidate):
                        if identity and identity not in infrastructure['cleanup']:
                            infrastructure['cleanup'].append(identity)
        db.expire_all()
        runtime = db.get(OpenCodeRuntime, runtime_id)
        assert runtime.status=='running' and runtime.config_version>0
        operation=db.query(RuntimeOperation).filter_by(runtime_id=runtime_id).one()
        assert operation.state=='complete' and operation.finished_at is not None
        assert operation.candidate_container_id==runtime.container_id and operation.previous_container_id is None
        container=docker_service._client().containers.get(runtime.container_id)
        inspected=json.loads(docker('inspect',container.id).stdout)[0]
        assert inspected['Name']==f'/skycloud-opencode-w{ws_id}-u{user_id}-g{operation.generation}'
        assert inspected['State']['Running'] is True
        mount_rows = docker('exec', container.id, 'cat', '/proc/mounts').stdout.splitlines()
        temporary = next(row for row in mount_rows if row.split()[1] == '/tmp')
        native = next(row for row in mount_rows if row.split()[1] == '/tmp/.native')
        assert 'noexec' in temporary.split()[3].split(',')
        assert 'noexec' not in native.split()[3].split(',')
        assert 'size=65536k' in native.split()[3].split(',')
    app = FastAPI(); app.include_router(router, prefix='/api'); app.include_router(ui_gateway.router)
    def database():
        with factory() as db:
            yield db
    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_session_factory] = lambda: factory
    auth = {'Authorization': 'Bearer ' + generate_token(user_id, 0)}
    if revocation == 'refresh-replay':
        import jwt
        from app.features.auth import refresh_sessions
        from app.infra.extensions import SECRET_KEY
        monkeypatch.setattr(refresh_sessions, 'redis_client', infrastructure['redis'])
        with factory() as db:
            refresh_raw, access = refresh_sessions.create(db, jwt.decode(auth['Authorization'][7:], SECRET_KEY, algorithms=['HS256']))
            replacement, _, _ = refresh_sessions.refresh(db, refresh_raw)
        assert replacement != refresh_raw
        auth = {'Authorization': 'Bearer ' + access['token']}
    if os.getenv('RUN_RUNTIME_UI_BROWSER') == '1':
        from tests.integration.runtime_ui_browser import exercise_browser
        exercise_browser(app, monkeypatch, user_id, ws_id, auth, tmp_path)
        monkeypatch.setenv('APP_ORIGIN', 'http://app.skycloud.localhost')
        monkeypatch.setenv('RUNTIME_UI_ORIGIN_TEMPLATE', 'http://r{runtime_id}.skycloud.localhost:8081')
    with TestClient(app) as client:
        issued = client.post(f'/api/workspace/{ws_id}/opencode/ui-ticket', headers=auth)
        assert issued.status_code == 200, issued.text
        parsed = urlsplit(issued.json()['data']['launch_url']); host = parsed.netloc
        launch = client.get('/runtime-ui/__skycloud/launch?' + parsed.query,
                            headers={'Host': host}, follow_redirects=False)
        assert launch.status_code == 303
        headers = {'Host': host, 'Origin': 'http://' + host,
                   'Cookie': ui_tickets.COOKIE + '=' + launch.cookies[ui_tickets.COOKIE]}
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            health = client.get('/runtime-ui/global/health', headers=headers)
            if health.status_code == 200:
                break
            time.sleep(0.2)
        assert health.status_code == 200, health.text
        created = client.post('/runtime-ui/pty?directory=%2Fworkspace', headers=headers,
            json={'command': 'sh', 'args': [], 'cwd': '/workspace', 'title': 'Real terminal', 'env': {}})
        if created.status_code != 200:
            logs = docker('logs', container.id, check=False)
            (FIXTURE_DIR / 'skycloud-ui-terminal-runtime.log').write_text(logs.stdout + logs.stderr)
        assert created.status_code == 200, created.text
        pty_id = created.json()['id']
        path = f'/runtime-ui/pty/{pty_id}/connect?directory=%2Fworkspace&cursor=0'
        if use_ticket:
            ticket = client.post(f'/runtime-ui/pty/{pty_id}/connect-token?directory=%2Fworkspace',
                                 headers={**headers, 'x-opencode-ticket': '1'})
            assert ticket.status_code == 200, ticket.text
            path += '&ticket=' + ticket.json()['ticket']
        with client.websocket_connect(path, headers=headers) as socket:
            # The expected contiguous output isn't present in the input command.
            socket.send_text("printf 'SKYCLOUD_UID_%s\\n' \"$(id -u)\"\r")
            output = bytearray()
            watchdog = threading.Timer(10, lambda: socket.close(1000))
            watchdog.start()
            try:
                while b'SKYCLOUD_UID_10001' not in output:
                    message = socket.receive()
                    assert message['type'] == 'websocket.send', message
                    chunk = message.get('bytes')
                    output.extend(chunk if chunk is not None else message.get('text', '').encode())
                    assert len(output) < 1024 * 1024
            finally:
                watchdog.cancel()
            with factory() as db:
                if revocation == 'membership':
                    db.execute(update(WorkspaceMember).where(WorkspaceMember.workspace_id == ws_id,
                        WorkspaceMember.user_id == user_id).values(role='viewer'))
                    db.commit()
                else:
                    from fastapi import HTTPException
                    with pytest.raises(HTTPException) as replay:
                        refresh_sessions.refresh(db, refresh_raw)
                    assert replay.value.status_code == 401
                    assert db.query(WorkspaceMember.role).filter_by(workspace_id=ws_id,user_id=user_id).scalar() == 'editor'
            while True:
                message = socket.receive()
                if message['type'] == 'websocket.close':
                    assert message['code'] == 1008
                    break
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(path, headers=headers):
                pass
        assert exc.value.code == 1008
        assert client.post(f'/api/workspace/{ws_id}/opencode/ui-ticket', headers=auth).status_code == (403 if revocation == 'membership' else 401)
    container.remove()
