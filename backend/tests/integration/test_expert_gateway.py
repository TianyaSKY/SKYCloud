"""Real isolated OpenCode -> model gateway -> synthetic provider flow."""
import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import uuid

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker
import uvicorn

from tests.integration.test_runtime_security import infrastructure, controller, docker
from tests.integration.test_migrations import postgres
from entry.migrate import upgrade
from app.features.assistant import repository, service
from app.features.runtime_llm import quota
from app.features.workspace import docker_service
from app.infra.extensions import get_db
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember

pytestmark = pytest.mark.skipif(os.getenv('RUN_EXPERT_GATEWAY_TESTS') != '1', reason='Opt-in isolated actual expert/model gateway integration')


@contextmanager
def serve(app):
    sock = socket.socket(); sock.bind(('0.0.0.0', 0)); sock.listen(128)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, lifespan='off', log_level='error', access_log=False))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True); thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started
        yield port
    finally:
        server.should_exit = True; thread.join(10); sock.close()
        assert not thread.is_alive()


def test_actual_expert_through_controller_gateway_and_mcp(infrastructure, controller, postgres, monkeypatch, tmp_path):
    from entry.llm_gateway import app as gateway
    upgrade(postgres)
    factory = sessionmaker(bind=postgres)
    provider_key = 'synthetic-provider-secret-' + uuid.uuid4().hex
    model = 'skycloud-mock-model'
    requests = []
    provider = FastAPI()

    @provider.post('/v1/chat/completions')
    async def complete(request: Request):
        body = await request.json()
        assert request.headers.get('authorization') == 'Bearer ' + provider_key
        assert body['model'] == model
        requests.append(body)
        reply = {'id': 'chatcmpl-test', 'object': 'chat.completion', 'created': int(time.time()), 'model': model,
                 'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'E2E_GATEWAY_OK'}, 'finish_reason': 'stop'}],
                 'usage': {'prompt_tokens': 20, 'completion_tokens': 5, 'total_tokens': 25}}
        if not body.get('stream'):
            return reply
        async def chunks():
            base = {key: value for key, value in reply.items() if key not in ('choices', 'usage')}
            base['object'] = 'chat.completion.chunk'
            for delta, finish in [({'role': 'assistant', 'content': ''}, None), ({'content': 'E2E_GATEWAY_OK'}, None), ({}, 'stop')]:
                yield 'data: ' + json.dumps({**base, 'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}) + '\n\n'
            yield 'data: ' + json.dumps({**base, 'choices': [], 'usage': reply['usage']}) + '\n\n'
            yield 'data: [DONE]\n\n'
        return StreamingResponse(chunks(), media_type='text/event-stream')

    def database():
        with factory() as db:
            yield db
    gateway.dependency_overrides[get_db] = database
    monkeypatch.setattr(quota, 'redis_client', infrastructure['redis'])
    monkeypatch.setenv('CHAT_API_MODEL', model)
    monkeypatch.setenv('CHAT_API_KEY', provider_key)
    monkeypatch.setenv('OPENCODE_LLM_GATEWAY_URL', 'http://skycloud-llm-gateway:5002/v1')
    monkeypatch.setenv('OPENCODE_MCP_URL', 'http://backend-mcp:5001/mcp')
    with factory() as db:
        for table in ('users', 'workspaces', 'opencode_runtimes'):
            db.execute(text("SELECT setval(pg_get_serial_sequence(:table, 'id'), :value, false)"), {'table': table, 'value': int(uuid.uuid4().hex[:7], 16) + 1})
        user = User(username='expert-gateway-' + uuid.uuid4().hex[:8], password_hash='synthetic-unused-hash', role='common')
        db.add(user); db.flush()
        workspace = Workspace(name='expert gateway test', owner_id=user.id)
        db.add(workspace); db.flush()
        db.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role='admin')); db.commit()
        user_id, ws_id = user.id, workspace.id
    runtime_name = f'skycloud-opencode-w{ws_id}-u{user_id}'
    assert docker('inspect', runtime_name, check=False).returncode != 0
    infrastructure['cleanup'].append(runtime_name)
    bridge = infrastructure['target'] + '-gateway'
    infrastructure['cleanup'].append(bridge)
    process = None
    observed = []
    try:
        with serve(provider) as provider_port, serve(gateway) as gateway_port:
            monkeypatch.setenv('CHAT_API_URL', f'http://127.0.0.1:{provider_port}/v1')
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', 0)); mcp_port = probe.getsockname()[1]
            environment = {**os.environ, 'DATABASE_URL': postgres.url.render_as_string(hide_password=False),
                           'REDIS_HOST': '127.0.0.1', 'REDIS_PORT': str(infrastructure['redis'].connection_pool.connection_kwargs['port']),
                           'MCP_PORT': str(mcp_port)}
            with (tmp_path / 'mcp.log').open('w') as output:
                process = subprocess.Popen([sys.executable, '-m', 'entry.mcp_run'], env=environment, stdout=output, stderr=subprocess.STDOUT)
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    assert process.poll() is None, (tmp_path / 'mcp.log').read_text()[-4000:]
                    try:
                        if httpx.get(f'http://127.0.0.1:{mcp_port}/mcp', timeout=1).status_code == 406:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.1)
                else:
                    raise AssertionError('MCP subprocess did not start')
                config = tmp_path / 'proxy.conf'
                config.write_text(f'''server {{ listen 5002; location / {{ proxy_pass http://host.docker.internal:{gateway_port}; proxy_buffering off; proxy_read_timeout 180s; }} }}
server {{ listen 5001; location / {{ proxy_pass http://host.docker.internal:{mcp_port}; proxy_set_header Host backend-mcp:5001; proxy_buffering off; proxy_read_timeout 180s; }} }}''')
                docker('create', '--name', bridge, '--network', infrastructure['infra'], '--entrypoint', 'nginx', '-v', str(config) + ':/etc/nginx/conf.d/default.conf:ro', 'skycloud-frontend:locked-test', '-g', 'daemon off;')
                docker('network', 'connect', '--gw-priority', '-1', '--alias', 'skycloud-llm-gateway', '--alias', 'backend-mcp', infrastructure['runtime'], bridge)
                docker('start', bridge)
                assert docker('inspect', '--format', '{{.State.Running}}', bridge).stdout.strip() == 'true'
                with factory() as db:
                    workspace = db.get(Workspace, ws_id)
                    conversation = repository.create_conversation(db, ws_id, user_id, 'expert', 'gateway task'); db.commit()
                    conversation, run, history = service.prepare_run(db, workspace, user_id, conversation.id, '只输出 E2E_GATEWAY_OK，不调用任何工具。')
                    async def collect():
                        async for event in service.stream_prepared_run(db, workspace, user_id, conversation, run, history):
                            observed.append(event)
                        return observed
                    events = asyncio.run(asyncio.wait_for(collect(), 120))
                    db.refresh(run)
                    assert run.status == 'completed', [(event.type, event.payload) for event in events]
                    assert 'E2E_GATEWAY_OK' in repository.get_messages(db, conversation.id)[-1].content
                    assert requests, 'No model request traversed the real gateway'
                    assert all(0 < body.get('max_tokens', body.get('max_completion_tokens', 0)) <= 4096 for body in requests)
                    assert any(event.type == 'done' for event in events)
                    runtime = docker_service.runtime_service.get_runtime(db, ws_id, user_id)
                    container = docker_service._client().containers.get(runtime.container_id)
                    runtime_config = container.read_config()
                    assert provider_key not in json.dumps(runtime_config)
                    raw = runtime_config['provider'][docker_service.OPENCODE_CHAT_PROVIDER_ID]['options']['apiKey']
                    from app.features.runtime_llm import tokens
                    assert tokens.validate_token(db, raw)['type'] == 'runtime_llm'
                    assert list(infrastructure['redis'].scan_iter('runtime-llm-quota:*:requests'))
                    from app.features.assistant.clients.opencode_client import OpenCodeClient
                    from app.features.assistant.clients.opencode_auth import server_password, server_username
                    async def mcp_status():
                        async with OpenCodeClient(docker_service.get_runtime_base_url(workspace, user_id, runtime), server_username(), server_password(runtime.id, user_id, ws_id)) as client:
                            return await client.get_mcp_status()
                    assert asyncio.run(mcp_status())['SKYCLOUD']['status'] == 'connected'

    finally:
        Path('/private/tmp/skycloud-expert-gateway-results.json').write_text(json.dumps([{'fields': list(body), 'max_tokens': body.get('max_tokens')} for body in requests]))
        Path('/private/tmp/skycloud-expert-gateway-events.json').write_text(json.dumps([{'type': event.type, 'payload': event.payload} for event in observed], ensure_ascii=False))
        for name, label in [(runtime_name, 'runtime'), (bridge, 'proxy')]:
            logs = docker('logs', name, check=False)
            Path(f'/private/tmp/skycloud-expert-gateway-{label}.log').write_text(logs.stdout + logs.stderr)
        gateway.dependency_overrides.pop(get_db, None)
        if process is not None:
            process.terminate()
            try: process.wait(10)
            except subprocess.TimeoutExpired: process.kill(); process.wait(5)
