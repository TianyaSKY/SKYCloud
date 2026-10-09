"""Manual CUA verification against actual frontend/nginx, API, PG, Redis and MinIO.

RUN_FULL_BROWSER=1 SKYCLOUD_TEST_RUNTIME_IMAGE=<builder output> SKYCLOUD_TEST_MINIO_IMAGE=<builder output> pytest tests/integration/test_full_browser.py
Read the synthetic fixture URL from /private/tmp/skycloud-full-browser.json;
record verified in its completion file only after browser checks succeed.
SKYCLOUD_BROWSER_STARTUP_ONLY=1 validates fixture startup without claiming browser checks.
"""
import json
import os
from pathlib import Path
import socket
import threading
import time
import uuid

import httpx
import pytest
import uvicorn
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text

from tests.integration.test_runtime_security import infrastructure, controller, docker
from tests.integration.test_migrations import postgres
from tests.integration.test_upload_storage import minio_storage
from entry.migrate import upgrade
from app.infra.extensions import get_db, get_session_factory
from app.infra.schema import check_schema
from app.features.auth import refresh_sessions, user_service
from app.features.workspace import docker_service, ui_gateway, ui_tickets
from app.models.workspace import Workspace
from app.models.opencode_runtime import OpenCodeRuntime
from app.models.runtime_operation import RuntimeOperation

pytestmark = pytest.mark.skipif(os.getenv('RUN_FULL_BROWSER') != '1', reason='Opt-in manual full browser verification')


def free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def test_actual_frontend_auth_and_runtime(infrastructure, controller, postgres, minio_storage, monkeypatch, tmp_path):
    from app.api.factory import create_fastapi_app
    from app.infra import cache
    root = Path(__file__).resolve().parents[3]
    assert (root / 'frontend/dist/index.html').is_file(), 'Build the production frontend first'
    nginx_image=next(line.split()[1] for line in (root/'frontend/Dockerfile').read_text().splitlines()
                     if line.startswith('FROM nginx:'))
    assert '@sha256:' in nginx_image, 'Browser fixture must use pinned production nginx'
    upgrade(postgres); check_schema(postgres)
    factory = sessionmaker(bind=postgres)
    from app.infra import storage
    from app.features.file import service as upload_service
    from app.features.folder import change_log
    monkeypatch.setattr(storage, '_storage_client', minio_storage)
    monkeypatch.setattr(upload_service, 'UPLOAD_FOLDER', str(tmp_path / 'uploads'))
    monkeypatch.setattr(upload_service, 'MULTIPART_ROOT', str(tmp_path / 'parts'))
    monkeypatch.setattr(change_log, 'SessionLocal', factory)
    for module in (refresh_sessions, ui_tickets, cache):
        monkeypatch.setattr(module, 'redis_client', infrastructure['redis'])
    monkeypatch.setattr(ui_gateway, 'SessionLocal', factory)
    username, password = 'browser-' + uuid.uuid4().hex[:12], 'disposable-browser-password-32'
    with factory() as db:
        for table in ('users', 'workspaces', 'opencode_runtimes'):
            db.execute(text("SELECT setval(pg_get_serial_sequence(:table, 'id'), :value, false)"),
                       {'table': table, 'value': int(uuid.uuid4().hex[:7], 16) + 1})
        user = user_service.create_user(db, {'username': username, 'password': password})
        user_id = user.id
        workspace = db.query(Workspace).filter_by(owner_id=user.id).first()
        ws_id = workspace.id
        runtime = OpenCodeRuntime(workspace_id=ws_id, user_id=user.id, status='stopped')
        db.add(runtime); db.commit()
        name = f'skycloud-opencode-w{ws_id}-u{user_id}'
        assert docker('inspect', name, check=False).returncode != 0
        infrastructure['cleanup'].append(name)
        runtime_id=runtime.id
        try:
            docker_service.start(db,workspace,user_id)
        finally:
            with factory() as cleanup_db:
                for generation,candidate in cleanup_db.query(RuntimeOperation.generation,
                        RuntimeOperation.candidate_container_id).filter_by(workspace_id=ws_id,user_id=user_id).all():
                    for identity in (f'skycloud-opencode-w{ws_id}-u{user_id}-g{generation}',candidate):
                        if identity and identity not in infrastructure['cleanup']:
                            infrastructure['cleanup'].append(identity)
        db.expire_all()
        runtime=db.get(OpenCodeRuntime,runtime_id)
        operation=db.query(RuntimeOperation).filter_by(runtime_id=runtime_id).one()
        assert runtime.status=='running' and runtime.config_version>0
        assert operation.state=='complete' and operation.candidate_container_id==runtime.container_id
    app = create_fastapi_app()
    def database():
        with factory() as db:
            yield db
    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_session_factory] = lambda: factory
    api_socket = socket.socket(); api_socket.bind(('127.0.0.1', 0)); api_socket.listen(128)
    api_port = api_socket.getsockname()[1]
    parent_port, ui_port = free_port(), free_port()
    parent = f'http://app.skycloud.localhost:{parent_port}'
    monkeypatch.setenv('APP_ORIGIN', parent)
    monkeypatch.setenv('RUNTIME_UI_ORIGIN_TEMPLATE', f'http://r{{runtime_id}}.skycloud.localhost:{ui_port}')
    server = uvicorn.Server(uvicorn.Config(app, lifespan='off', log_level='warning', access_log=False,
                                         ws_max_size=1024 * 1024, ws_max_queue=4))
    # The test explicitly migrated/checked its database above; production
    # startup and migration behavior have their own real integration checks.
    thread = threading.Thread(target=lambda: server.run(sockets=[api_socket]), daemon=True)
    thread.start()
    frontend = infrastructure['target'] + '-frontend'
    infrastructure['cleanup'].append(frontend)
    config = tmp_path / 'default.conf.template'
    config.write_text((root / 'frontend/nginx.conf').read_text().replace('backend-api:5000', f'host.docker.internal:{api_port}'))
    metadata = Path('/private/tmp/skycloud-full-browser.json')
    completion = tmp_path / 'browser-completed'
    try:
        docker('run', '-d', '--name', frontend, '--network', infrastructure['infra'], '-p', f'127.0.0.1:{parent_port}:80',
               '-p', f'127.0.0.1:{ui_port}:8081', '-e', f'RUNTIME_UI_CSP_SOURCE=http://*.skycloud.localhost:{ui_port}',
               '-v', str(config) + ':/etc/nginx/templates/default.conf.template:ro',
               '-v', str(root / 'frontend/dist') + ':/usr/share/nginx/html:ro', nginx_image)
        deadline = time.monotonic() + 15
        ready = None
        while time.monotonic() < deadline:
            try:
                ready = httpx.get(f'http://127.0.0.1:{parent_port}/api/health', timeout=2, trust_env=False)
                if ready.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(.2)
        if ready is None or ready.status_code != 200:
            logs = docker('logs', frontend, check=False)
            Path('/private/tmp/skycloud-full-browser-nginx.log').write_text(logs.stdout + logs.stderr)
        assert ready is not None and ready.status_code == 200, 'Isolated nginx cannot reach the loopback API'
        document = httpx.get(f'http://127.0.0.1:{parent_port}/', timeout=3, trust_env=False)
        assert "script-src 'self'" in document.headers['content-security-policy']
        assert 'unsafe-eval' not in document.headers['content-security-policy']
        if os.getenv('SKYCLOUD_BROWSER_STARTUP_ONLY')=='1':
            return
        metadata.write_text(json.dumps({'url': parent, 'username': username, 'password': password,
            'workspace_id': ws_id, 'completion_file': str(completion)}))
        deadline = time.monotonic() + 1800
        while not completion.exists() and time.monotonic() < deadline:
            assert thread.is_alive(), 'API stopped during browser verification'
            time.sleep(.2)
        assert completion.exists() and completion.read_text().strip() == 'verified'
    finally:
        metadata.unlink(missing_ok=True)
        server.should_exit = True
        thread.join(5); api_socket.close()
