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
from app.infra.extensions import get_db, get_session_factory
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember
from app.models.runtime_operation import RuntimeOperation

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


@pytest.mark.parametrize('permission_response,crash_before_completion,missing_candidate', [(None,False,None),(None,True,None),('once',False,None),('reject',False,None),(None,False,'preparing'),(None,False,'switched'),(None,False,'conversation'),('unconfirmed',False,None),(None,False,'prompt-refusal'),(None,False,'completed-disconnect'),('demote',False,None),('remove',False,None),('auth-version',False,None)],ids=['cold','crash','allow','reject','missing-preparing','missing-switched','conversation-replacement','permission-unconfirmed','prior-answer-refusal','completed-disconnect','permission-demote','permission-remove','http-auth-version'])
def test_actual_expert_through_controller_gateway_and_mcp(infrastructure, controller, postgres, monkeypatch, tmp_path, permission_response,crash_before_completion,missing_candidate):
    from entry.llm_gateway import app as gateway
    if permission_response == 'unconfirmed':
        # Inject only the control acknowledgment boundary. Runtime, model/tool
        # request, permission persistence and subsequent abort use real services.
        from app.features.assistant.clients.opencode_client import OpenCodeClient
        original_json = OpenCodeClient._json
        async def unconfirmed_permission(client, method, path, **kwargs):
            if '/permissions/' in path:
                return False
            return await original_json(client,method,path,**kwargs)
        monkeypatch.setattr(OpenCodeClient,'_json',unconfirmed_permission)
    disconnects = []
    if missing_candidate == 'completed-disconnect':
        from app.features.assistant.clients.opencode_client import OpenCodeClient, OpenCodeClientError
        original_events = OpenCodeClient.stream_events
        async def disconnect_after_completion(client):
            events = original_events(client)
            try:
                async for event in events:
                    properties = event.get('properties',{})
                    status = properties.get('status',{})
                    if event.get('type') == 'session.status' and isinstance(status,dict) and status.get('type') == 'idle':
                        disconnects.append(True)
                        raise OpenCodeClientError('Injected event disconnect after provider completion')
                    yield event
            finally:
                await events.aclose()
        monkeypatch.setattr(OpenCodeClient,'stream_events',disconnect_after_completion)
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
        tool_request = permission_response is not None and any(tool.get('function', {}).get('name') == 'bash' for tool in body.get('tools', [])) and not any(message.get('role') == 'tool' for message in body['messages'])
        call = {'id': 'call_skycloud_test', 'type': 'function', 'function': {'name': 'bash', 'arguments': json.dumps({'command': "printf 'AUTHORIZED_ONLY'", 'description': 'Print synthetic test marker'})}}
        if tool_request:
            reply['choices'][0]['message'] = {'role': 'assistant', 'content': None, 'tool_calls': [call]}
            reply['choices'][0]['finish_reason'] = 'tool_calls'
        if not body.get('stream'):
            return reply
        async def chunks():
            base = {key: value for key, value in reply.items() if key not in ('choices', 'usage')}
            base['object'] = 'chat.completion.chunk'
            frames = [({'role': 'assistant', 'content': ''}, None), ({'tool_calls': [{'index': 0, **call}]}, None), ({}, 'tool_calls')] if tool_request else [({'role': 'assistant', 'content': ''}, None), ({'content': 'E2E_GATEWAY_OK'}, None), ({}, 'stop')]
            for delta, finish in frames:
                yield 'data: ' + json.dumps({**base, 'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}) + '\n\n'
            yield 'data: ' + json.dumps({**base, 'choices': [], 'usage': reply['usage']}) + '\n\n'
            yield 'data: [DONE]\n\n'
        return StreamingResponse(chunks(), media_type='text/event-stream')

    def database():
        with factory() as db:
            yield db
    gateway.dependency_overrides[get_db] = database
    gateway.dependency_overrides[get_session_factory] = lambda: factory
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
    boot_process = None
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
                docker('create', '--name', bridge, '--network', infrastructure['infra'], '--add-host','host.docker.internal:host-gateway', '--entrypoint', 'nginx', '-v', str(config) + ':/etc/nginx/conf.d/default.conf:ro', 'skycloud-frontend:locked-test', '-g', 'daemon off;')
                docker('network', 'connect', '--gw-priority', '-1', '--alias', 'skycloud-llm-gateway', '--alias', 'backend-mcp', infrastructure['runtime'], bridge)
                docker('start', bridge)
                assert docker('inspect', '--format', '{{.State.Running}}', bridge).stdout.strip() == 'true'
                if missing_candidate in ('preparing','switched'):
                    from app.features.workspace import runtime_operation_repository as operations
                    with factory() as bootstrap:
                        old = operations.admit(bootstrap,ws_id,user_id)
                        old_generation,rt_id = old.generation,int(old.runtime_id)
                        bootstrap.commit()
                        prepared = docker_service._client().prepare(ws_id,user_id,rt_id,old_generation)
                        if missing_candidate == 'switched':
                            operations.record_candidate(bootstrap,old_generation,prepared.id)
                            bootstrap.commit()
                            operations.switch_candidate(bootstrap,old_generation)
                            bootstrap.commit()
                    docker('rm','-f',prepared.id)
                    from app.infra.runtime_operations import recover_runtime_operations
                    recover_runtime_operations(session_factory=factory)
                    with factory() as observer:
                        assert observer.get(RuntimeOperation,old_generation).state == 'cancelled'
                        replacement = observer.query(RuntimeOperation).filter_by(workspace_id=ws_id,user_id=user_id,state='complete').one()
                        assert replacement.generation != old_generation and replacement.candidate_container_id != prepared.id
                if crash_before_completion:
                    marker = tmp_path / 'configured-generation'
                    # Interrupt an actual locked-SDK/API process after the
                    # external config and its SQL version commit, before the
                    # operation's completion transaction. No container mock.
                    boot_code = """
import json,sys,time
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.models.workspace import Workspace
from app.features.workspace import docker_service, runtime_operation_repository as operations
settings=json.load(sys.stdin)
engine=create_engine(settings['database'])
def interrupt_point(session,generation):
    Path(settings['marker']).write_text(generation)
    time.sleep(120)
operations.complete=interrupt_point
with Session(engine) as session:
    docker_service.start(session,session.get(Workspace,settings['workspace']),settings['user'])
"""
                    with (tmp_path / 'boot.log').open('w') as boot_output:
                        boot_process = subprocess.Popen([sys.executable,'-c',boot_code],env=environment,
                            stdin=subprocess.PIPE,stdout=boot_output,stderr=subprocess.STDOUT,text=True)
                        boot_process.stdin.write(json.dumps({'database':postgres.url.render_as_string(hide_password=False),
                            'workspace':ws_id,'user':user_id,'marker':str(marker)}))
                        boot_process.stdin.close()
                        deadline = time.monotonic()+40
                        while not marker.exists() and time.monotonic()<deadline:
                            assert boot_process.poll() is None,(tmp_path/'boot.log').read_text()[-2000:]
                            time.sleep(0.1)
                        assert marker.exists(),'Startup did not reach configured interruption point'
                        generation = marker.read_text()
                        boot_process.kill()
                        assert boot_process.wait(5) < 0
                    with factory() as observer:
                        pending = observer.get(RuntimeOperation,generation)
                        assert pending.state == 'switched' and pending.finished_at is None
                        candidate_id = pending.candidate_container_id
                    from app.infra.runtime_operations import recover_runtime_operations
                    assert recover_runtime_operations(session_factory=factory) == 1
                    with factory() as observer:
                        recovered = observer.get(RuntimeOperation,generation)
                        assert recovered.state == 'complete' and recovered.candidate_container_id == candidate_id
                with factory() as db:
                    workspace = db.get(Workspace, ws_id)
                    conversation = repository.create_conversation(db, ws_id, user_id, 'expert', 'gateway task'); db.commit()
                    if permission_response != 'auth-version':
                        conversation, run, history = service.prepare_run(db, workspace, user_id, conversation.id, '只输出 E2E_GATEWAY_OK，不调用任何工具。')
                    async def collect():
                        if permission_response == 'auth-version':
                            from app.features.assistant import router as assistant_router
                            from app.features.assistant.event_protocol import AssistantEvent
                            from app.features.auth.service import generate_token
                            from app.features.assistant.clients.opencode_client import OpenCodeClient
                            from app.models.assistant import AssistantRun
                            raw = generate_token(user_id, 0)
                            abort_acks, abort_closed, abort_errors, revoked, event_clients = [], [], [], [], []
                            actual_abort = OpenCodeClient.abort_session
                            actual_events = OpenCodeClient.stream_events
                            async def observed_events(client):
                                event_clients.append(client._client)
                                async for event in actual_events(client):
                                    yield event
                            monkeypatch.setattr(OpenCodeClient, 'stream_events', observed_events)
                            async def observed_abort(client, session_id):
                                abort_closed.append(client._client.is_closed)
                                assert client._client not in event_clients
                                try:
                                    result = await actual_abort(client, session_id)
                                except Exception as exc:
                                    abort_errors.append((type(exc).__name__, getattr(exc, 'status_code', None)))
                                    raise
                                abort_acks.append(result)
                                return result
                            monkeypatch.setattr(OpenCodeClient, 'abort_session', observed_abort)
                            app = FastAPI(); app.include_router(assistant_router.router)
                            app.dependency_overrides[get_db] = lambda: None
                            app.dependency_overrides[get_session_factory] = lambda: factory
                            async def revoke_version():
                                def update():
                                    with postgres.begin() as independent:
                                        independent.execute(text('UPDATE users SET auth_version=auth_version+1 WHERE id=:id'), {'id': user_id})
                                await asyncio.to_thread(update)
                            async def observed_app(scope, receive, send):
                                async def observe(message):
                                    await send(message)
                                    if message['type'] == 'http.response.body' and b'permission_required' in message.get('body', b'') and not revoked:
                                        revoked.append(True)
                                        await revoke_version()
                                await app(scope, receive, observe)
                            async with httpx.AsyncClient(transport=httpx.ASGITransport(observed_app), base_url='http://test') as client:
                                response = await client.post(f'/assistant/conversations/{conversation.id}/messages/stream',
                                    headers={'Authorization': 'Bearer '+raw, 'X-Workspace-Id': str(ws_id)},
                                    json={'query':'只输出 E2E_GATEWAY_OK，不调用任何工具。'})
                            assert response.status_code == 200 and raw not in response.text
                            observed.extend(AssistantEvent(**json.loads(line[6:])) for line in response.text.splitlines() if line.startswith('data: '))
                            assert revoked and abort_acks and all(abort_acks) and not any(abort_closed), (abort_closed, abort_errors)
                            result = db.get(AssistantRun, int(response.headers['X-Assistant-Run-Id']))
                            assert result.status == 'cancelled' and result.pending_permission_id is None
                            assert observed[-1].type == 'cancelled'
                            assert service.run_registry.get(result.id) is None
                            with postgres.connect() as independent:
                                assert independent.scalar(text('SELECT auth_version FROM users WHERE id=:id'), {'id':user_id}) == 1
                                assert independent.scalar(text('SELECT role FROM workspace_members WHERE workspace_id=:ws AND user_id=:id'), {'ws':ws_id,'id':user_id}) == 'admin'
                            assert not any('AUTHORIZED_ONLY' in str(message.get('content','')) for body in requests for message in body['messages'] if message.get('role') == 'tool')
                            return observed
                        async for event in service.stream_run(factory, ws_id, user_id, conversation.id, run.id, history):
                            observed.append(event)
                            if event.type == 'permission_required':
                                assert permission_response is not None
                                assert "printf 'AUTHORIZED_ONLY'" in event.payload['tool']
                                def respond_from_worker():
                                    with factory() as responder:
                                        restored = responder.get(type(run), run.id)
                                        assert restored.to_dict()['pending_permission']['tool'] == event.payload['tool']
                                        if permission_response in ('demote','remove'):
                                            responder.execute(text("UPDATE workspace_members SET role='viewer' WHERE workspace_id=:ws AND user_id=:user" if permission_response == 'demote' else "DELETE FROM workspace_members WHERE workspace_id=:ws AND user_id=:user"),{'ws':ws_id,'user':user_id})
                                            responder.commit()
                                        else:
                                            service.respond_permission(responder, responder.get(Workspace, ws_id), user_id, run.id, event.payload['permission_id'], 'once' if permission_response == 'unconfirmed' else permission_response)
                                await asyncio.to_thread(respond_from_worker)
                        return observed
                    events = asyncio.run(asyncio.wait_for(collect(), 120))
                    if permission_response == 'auth-version':
                        return
                    db.refresh(run)
                    if permission_response in ('demote','remove'):
                        assert run.status == 'cancelled'
                        assert run.pending_permission_id is None
                        assert any(event.type == 'cancelled' for event in events)
                        assert not any(event.payload.get('content') == '权限已响应，继续执行' for event in events)
                        assert not any('AUTHORIZED_ONLY' in str(message.get('content','')) for body in requests for message in body['messages'] if message.get('role') == 'tool')
                        return
                    if permission_response == 'unconfirmed':
                        assert run.status == 'failed'
                        assert run.pending_permission_id is None
                        assert any(event.type == 'error' for event in events)
                        assert not any(event.payload.get('content') == '权限已响应，继续执行' for event in events)
                        assert not any('AUTHORIZED_ONLY' in str(message.get('content','')) for body in requests for message in body['messages'] if message.get('role') == 'tool')
                        return
                    if run.status != ('cancelled' if permission_response == 'reject' else 'completed') and run.opencode_session_id:
                        from app.features.assistant.clients.opencode_client import OpenCodeClient
                        from app.features.assistant.clients.opencode_auth import server_password,server_username
                        current_runtime = docker_service.runtime_service.get_runtime(db,ws_id,user_id)
                        async def failure_messages():
                            async with OpenCodeClient(docker_service.get_runtime_base_url(workspace,user_id,current_runtime),server_username(),server_password(current_runtime.id,user_id,ws_id)) as failure_client:
                                return await failure_client.get_messages(run.opencode_session_id)
                        failures = asyncio.run(failure_messages())
                        import re
                        detail = json.dumps([{key:message.get('info',{}).get(key) for key in ('id','role','parentID','time','finish','error')} for message in failures])
                        detail = re.sub(r'eyJ[A-Za-z0-9_.-]+','[redacted]',detail.replace(provider_key,'[redacted]'))
                        raise AssertionError('OpenCode recovered task failed: '+detail[:1500])
                    assert run.status == ('cancelled' if permission_response == 'reject' else 'completed'), [(event.type, event.payload) for event in events]
                    if missing_candidate == 'completed-disconnect':
                        assert disconnects, 'Completion disconnect boundary was not exercised'
                    if permission_response != 'reject':
                        assert 'E2E_GATEWAY_OK' in repository.get_messages(db, conversation.id)[-1].content
                    assert requests, 'No model request traversed the real gateway'
                    assert all(0 < body.get('max_tokens', body.get('max_completion_tokens', 0)) <= 4096 for body in requests)
                    assert any(event.type == ('cancelled' if permission_response == 'reject' else 'done') for event in events)
                    if permission_response is not None:
                        assert any(event.type == 'permission_required' for event in events)
                        tool_replies = [message.get('content', '') for body in requests for message in body['messages'] if message.get('role') == 'tool']
                        if permission_response == 'once':
                            assert any('AUTHORIZED_ONLY' in str(reply) for reply in tool_replies)

                    if missing_candidate == 'prompt-refusal':
                        from app.features.assistant.clients.opencode_client import OpenCodeClient, OpenCodeClientError
                        first_session = run.opencode_session_id
                        original_json = OpenCodeClient._json
                        async def refuse_prompt(client,method,path,**kwargs):
                            if path.endswith('/prompt_async'):
                                raise OpenCodeClientError('OpenCode returned HTTP 503',503)
                            return await original_json(client,method,path,**kwargs)
                        monkeypatch.setattr(OpenCodeClient,'_json',refuse_prompt)
                        # Repeat the same question: content equality cannot
                        # distinguish the old answer from this refused attempt.
                        conversation, run, history = service.prepare_run(db,workspace,user_id,conversation.id,'只输出 E2E_GATEWAY_OK，不调用任何工具。')
                        observed.clear()
                        events = asyncio.run(asyncio.wait_for(collect(),30))
                        db.refresh(run)
                        assert run.status == 'failed'
                        assert run.opencode_session_id == first_session
                        assert repository.get_messages(db,conversation.id)[-1].content == ''
                        assert any(event.type == 'error' for event in events)
                        assert not any(event.type == 'token' for event in events)
                        return

                    if missing_candidate == 'conversation':
                        first_session = run.opencode_session_id
                        first_runtime = docker_service.runtime_service.get_runtime(db,ws_id,user_id)
                        first_container = first_runtime.container_id
                        first_generation = db.query(RuntimeOperation).filter_by(state='complete').one().generation
                        docker('rm','-f',first_container)  # Lose actual OpenCode session storage.
                        request_count = len(requests)
                        conversation, run, history = service.prepare_run(db,workspace,user_id,conversation.id,'续接历史，只输出 E2E_GATEWAY_OK，不调用工具。')
                        assert len(history) == 2
                        observed.clear()
                        events = asyncio.run(asyncio.wait_for(collect(),120))
                        db.refresh(run); db.refresh(conversation)
                        assert run.status == 'completed', [(event.type,event.payload) for event in events]
                        assert conversation.opencode_session_id == run.opencode_session_id != first_session
                        assert len(repository.get_messages(db,conversation.id)) == 4
                        assert 'E2E_GATEWAY_OK' in repository.get_messages(db,conversation.id)[-1].content
                        # The new provider session receives committed SQL history
                        # before the second user prompt, across the actual gateway.
                        replayed = json.dumps([body['messages'] for body in requests[request_count:]],ensure_ascii=False)
                        assert '只输出 E2E_GATEWAY_OK，不调用任何工具。' in replayed
                        assert '续接历史' in replayed
                        db.expire_all()
                        restored_runtime = docker_service.runtime_service.get_runtime(db,ws_id,user_id)
                        assert restored_runtime.container_id != first_container
                        assert db.get(RuntimeOperation,first_generation).state == 'cancelled'

                    runtime = docker_service.runtime_service.get_runtime(db, ws_id, user_id)
                    operation = db.query(RuntimeOperation).filter_by(workspace_id=ws_id,user_id=user_id,state='complete').one()
                    assert operation.state == 'complete' and operation.finished_at is not None
                    assert operation.candidate_container_id == runtime.container_id
                    container = docker_service._client().containers.get(runtime.container_id)
                    runtime_config = container.read_config()
                    assert provider_key not in json.dumps(runtime_config)
                    raw = runtime_config['provider'][docker_service.OPENCODE_CHAT_PROVIDER_ID]['options']['apiKey']
                    from app.features.runtime_llm import tokens
                    assert tokens.validate_token(db, raw)['type'] == 'runtime_llm'
                    assert list(infrastructure['redis'].scan_iter('runtime-llm-quota:*:requests'))
                    # Exercise the existing-runtime credential refresh with
                    # independent worker Sessions as well as cold startup.
                    from app.features.assistant.engines.expert_engine import ExpertEngine
                    async def refresh_existing():
                        engine = ExpertEngine(session=db, conversation=conversation, run=run)
                        return [item async for item in engine._ensure_runtime(ws_id, user_id)]
                    refreshed = asyncio.run(refresh_existing())
                    assert refreshed[0][1].payload['runtime_status'] == 'running'
                    assert refreshed[-1][0].runtime_id == runtime.id
                    from app.features.assistant.clients.opencode_client import OpenCodeClient
                    from app.features.assistant.clients.opencode_auth import server_password, server_username
                    async def mcp_status():
                        async with OpenCodeClient(docker_service.get_runtime_base_url(workspace, user_id, runtime), server_username(), server_password(runtime.id, user_id, ws_id)) as client:
                            return await client.get_mcp_status(), await client.get_messages(run.opencode_session_id)
                    connected, messages = asyncio.run(mcp_status())
                    assert connected['SKYCLOUD']['status'] == 'connected'
                    # Call the actual MCP search tool over its protocol using
                    # the latest scoped Runtime credential after live refresh.
                    mcp_headers = container.read_config()['mcp']['SKYCLOUD']['headers']
                    # Parent unit conftest replaces the MCP package. Run the
                    # locked SDK in an independent process, like the server.
                    client_code = """
import asyncio, json, sys, httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
settings = json.load(sys.stdin)
async def main():
    async with httpx.AsyncClient(headers=settings['headers'], trust_env=False, timeout=20) as client:
        async with streamable_http_client(settings['url'], http_client=client) as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                result = await session.call_tool('search_files', {'query': 'no-matching-test-file', 'search_type': 'fuzzy'})
                assert not result.isError
                print(json.dumps(json.loads(result.content[0].text)))
asyncio.run(main())
"""
                    checked = subprocess.run([sys.executable, '-c', client_code],
                        input=json.dumps({'url': f'http://127.0.0.1:{mcp_port}/mcp', 'headers': mcp_headers}),
                        capture_output=True, text=True, timeout=30)
                    assert checked.returncode == 0, checked.stderr[-2000:]
                    search_data = json.loads(checked.stdout)
                    assert search_data['items'] == [] and search_data['total'] == 0
                    from app.models.mcp_audit_log import McpAuditLog
                    with factory() as observer:
                        audit = observer.query(McpAuditLog).filter_by(workspace_id=ws_id, tool_name='search_files').one()
                        assert audit.success and audit.user_id == user_id and audit.runtime_id == runtime.id
                    # Validate the public DTOs against actual persisted expert results.
                    from app.features.assistant.schemas import (
                        AssistantConversationResponse, AssistantRunResponse, AssistantMessageResponse)
                    AssistantConversationResponse.model_validate(conversation.to_dict())
                    AssistantRunResponse.model_validate(run.to_dict())
                    for stored_message in repository.get_messages(db, conversation.id):
                        AssistantMessageResponse.model_validate(stored_message.to_dict())
                    if permission_response == 'reject':
                        states = [part['state'] for message in messages for part in message.get('parts', []) if part.get('type') == 'tool' and part.get('tool') == 'bash']
                        assert any(state.get('status') == 'error' and 'rejected' in state.get('error', '').lower() for state in states)
                        assert all('AUTHORIZED_ONLY' not in state.get('output', '') for state in states)

    finally:
        # New startup names are durable generations. Retain every admitted
        # name in fixture cleanup, including failures before candidate SQL.
        with factory() as cleanup_db:
            generations = cleanup_db.query(RuntimeOperation.generation).filter_by(workspace_id=ws_id,user_id=user_id).all()
            for (generation,) in generations:
                infrastructure['cleanup'].append(f'skycloud-opencode-w{ws_id}-u{user_id}-g{generation}')
            current = docker_service.runtime_service.get_runtime(cleanup_db,ws_id,user_id)
            if current and current.container_id:
                runtime_name = current.container_id
        Path('/private/tmp/skycloud-expert-gateway-results.json').write_text(json.dumps([{'fields': list(body), 'max_tokens': body.get('max_tokens')} for body in requests]))
        Path('/private/tmp/skycloud-expert-gateway-events.json').write_text(json.dumps([{'type': event.type, 'payload': event.payload} for event in observed], ensure_ascii=False))
        for name, label in [(runtime_name, 'runtime'), (bridge, 'proxy')]:
            logs = docker('logs', name, check=False)
            Path(f'/private/tmp/skycloud-expert-gateway-{label}.log').write_text(logs.stdout + logs.stderr)
        gateway.dependency_overrides.pop(get_db, None)
        if boot_process is not None and boot_process.poll() is None:
            boot_process.kill(); boot_process.wait(5)
        if process is not None:
            process.terminate()
            try: process.wait(10)
            except subprocess.TimeoutExpired: process.kill(); process.wait(5)
