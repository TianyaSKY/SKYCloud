from typing import Any, cast
# pyright: reportPrivateUsage=false
"""真实隔离 OpenCode 到模型网关再到合成提供商调用流测试."""
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

from tests.integration.fixture_paths import FIXTURE_DIR
from tests.integration.test_runtime_security import infrastructure, controller, docker
from tests.integration.test_migrations import postgres
from tests.integration.test_upload_storage import minio_storage
from entry.migrate import upgrade
from app.features.assistant import repository, service
from app.features.runtime_llm import quota
from app.features.workspace import docker_service
from app.infra.extensions import get_db, get_session_factory
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember
from app.models.runtime_operation import RuntimeOperation

__all__ = [
    "serve",
    "infrastructure",
    "controller",
    "docker",
    "postgres",
    "minio_storage",
]

pytestmark = pytest.mark.skipif(os.getenv('RUN_EXPERT_GATEWAY_TESTS') != '1', reason='Opt-in isolated actual expert/model gateway integration')


@contextmanager
def serve(app: Any):
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


@pytest.mark.parametrize('permission_response,crash_before_completion,missing_candidate', [(None,False,None),(None,True,None),('once',False,None),('reject',False,None),(None,False,'preparing'),(None,False,'switched'),(None,False,'conversation'),('unconfirmed',False,None),(None,False,'prompt-refusal'),(None,False,'completed-disconnect'),('demote',False,None),('remove',False,None),('auth-version',False,None),(None,False,'lease-loss'),('once',False,'command-lease-loss'),('once',False,'command-cancel'),('once',False,'command-crash'),(None,False,'lease-before-sql'),(None,False,'lease-set-reply'),('once',False,'command-cancel-retry'),(None,False,'paused-prompt'),('once',False,'permission-replay')],ids=['cold','crash','allow','reject','missing-preparing','missing-switched','conversation-replacement','permission-unconfirmed','prior-answer-refusal','completed-disconnect','permission-demote','permission-remove','http-auth-version','lease-loss','command-lease-loss','command-cancel','command-crash','lease-before-sql','lease-set-reply','command-cancel-retry','paused-prompt','permission-replay'])
def test_actual_expert_through_controller_gateway_and_mcp(infrastructure: Any, controller: Any, postgres: Any, monkeypatch: Any, tmp_path: Any, permission_response: Any,crash_before_completion: Any,missing_candidate: Any) -> None:
    from entry.llm_gateway import app as gateway
    if permission_response == 'unconfirmed':
        # 仅注入控制确认边界. 运行时、模型/工具请求、
        # 权限持久化与后续中止均使用真实服务.
        from app.features.assistant.clients.opencode_client import OpenCodeClient
        original_json = OpenCodeClient._json
        async def unconfirmed_permission(client: Any, method: Any, path: Any, **kwargs: Any):
            if '/permissions/' in path:
                return False
            return await original_json(client,method,path,**kwargs)
        monkeypatch.setattr(OpenCodeClient,'_json',unconfirmed_permission)
    permission_dispatches: list[tuple[Any, Any, Any]] = []
    if missing_candidate == 'permission-replay':
        from app.features.assistant.clients.opencode_client import OpenCodeClient
        actual_events = OpenCodeClient.stream_events
        actual_respond_permission = OpenCodeClient.respond_permission

        async def replay_permission_event(client: Any):
            async for event in actual_events(client):
                yield event
                event_dict: Any = event
                if event_dict.get('type') == 'permission.asked':
                    yield event

        async def record_permission_dispatch(
            client: Any, session_id: Any, permission_id: Any, response: Any, *, remember: Any=False,
        ):
            permission_dispatches.append((session_id, permission_id, response))
            return await actual_respond_permission(
                client,
                session_id,
                permission_id,
                response,
                remember=remember,
            )

        monkeypatch.setattr(OpenCodeClient, 'stream_events', replay_permission_event)
        monkeypatch.setattr(OpenCodeClient, 'respond_permission', record_permission_dispatch)
    lease_abort_acks: list[Any] = []
    provider_disconnected = threading.Event()
    replaced_lease_keys: list[Any] = []
    actual_abort: Any = None
    if missing_candidate in ('lease-loss','command-lease-loss','command-cancel','command-cancel-retry'):
        monkeypatch.setenv('ASSISTANT_RUNTIME_LOCK_TTL', '1')
        from app.features.assistant.clients.opencode_client import OpenCodeClient
        actual_abort = OpenCodeClient.abort_session
        async def observe_lease_abort(client: Any, session_id: Any):
            if missing_candidate == 'command-cancel-retry':
                from app.features.assistant.clients.opencode_client import OpenCodeClientError
                raise OpenCodeClientError('Synthetic control outage')
            result = await actual_abort(client, session_id)
            lease_abort_acks.append(result)
            return result
        monkeypatch.setattr(OpenCodeClient, 'abort_session', observe_lease_abort)
    disconnects: list[bool] = []
    if missing_candidate == 'completed-disconnect':
        from app.features.assistant.clients.opencode_client import OpenCodeClient, OpenCodeClientError
        original_events = OpenCodeClient.stream_events
        async def disconnect_after_completion(client: Any):
            events = original_events(client)
            try:
                async for event in events:
                    event_dict = cast(dict[str, Any], event)
                    properties = cast(dict[str, Any], event_dict.get('properties', {}))
                    status = cast(dict[str, Any], properties.get('status', {}))
                    if event_dict.get('type') == 'session.status' and status.get('type') == 'idle':
                        disconnects.append(True)
                        raise OpenCodeClientError('Injected event disconnect after provider completion')
                    yield event
            finally:
                aclose_fn: Any = getattr(events, 'aclose', None)
                if aclose_fn is not None:
                    await aclose_fn()
        monkeypatch.setattr(OpenCodeClient,'stream_events',disconnect_after_completion)
    upgrade(postgres)
    factory = sessionmaker(bind=postgres)
    provider_key = 'synthetic-provider-secret-' + uuid.uuid4().hex
    model = 'skycloud-mock-model'
    requests: list[dict[str, Any]] = []
    provider = FastAPI()

    @provider.post('/v1/chat/completions')
    async def complete(request: Request):
        body: dict[str, Any] = await request.json()
        assert request.headers.get('authorization') == 'Bearer ' + provider_key
        assert body['model'] == model
        requests.append(body)
        if missing_candidate == 'lease-loss':
            # 所有 SQL、Controller、OpenCode 和 Redis 操作均为原生.
            # 仅模型输出为合成数据并保持至调用方中止.
            keys = list(infrastructure['redis'].scan_iter('assistant:expert:runtime:*'))
            assert len(keys) == 1
            replaced_lease_keys.extend(keys)
            infrastructure['redis'].set(keys[0], 'successor-worker', ex=60)
            deadline = time.monotonic() + 10
            while not await request.is_disconnected():
                assert time.monotonic() < deadline, 'native model transport did not disconnect after lease loss'
                await asyncio.sleep(.01)
            provider_disconnected.set()
        reply: dict[str, Any] = {'id': 'chatcmpl-test', 'object': 'chat.completion', 'created': int(time.time()), 'model': model,
                 'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'E2E_GATEWAY_OK'}, 'finish_reason': 'stop'}],
                 'usage': {'prompt_tokens': 20, 'completion_tokens': 5, 'total_tokens': 25}}
        tool_request = permission_response is not None and any(tool.get('function', {}).get('name') == 'bash' for tool in body.get('tools', [])) and not any(message.get('role') == 'tool' for message in body['messages'])
        command = "printf 'AUTHORIZED_ONLY'"
        if missing_candidate in ('command-lease-loss','command-cancel','command-crash','command-cancel-retry'):
            command += """; sleep 4 & child=$!; printf '%s %s' "$$" "$child" > /tmp/skycloud-command-pids; wait "$child"; printf LATE > /tmp/skycloud-command-late"""
        if missing_candidate in ('command-crash','command-cancel-retry'):
            command = command.replace('sleep 4 &', 'sleep 60 &')
        call = {'id': 'call_skycloud_test', 'type': 'function', 'function': {'name': 'bash', 'arguments': json.dumps({'command': command, 'description': 'Print synthetic test marker'})}}
        if tool_request:
            reply['choices'][0]['message'] = {'role': 'assistant', 'content': None, 'tool_calls': [call]}
            reply['choices'][0]['finish_reason'] = 'tool_calls'
        if not body.get('stream'):
            return reply
        async def chunks():
            base = {key: value for key, value in reply.items() if key not in ('choices', 'usage')}
            base['object'] = 'chat.completion.chunk'
            frames: list[tuple[dict[str, Any], str | None]] = [({'role': 'assistant', 'content': ''}, None), ({'tool_calls': [{'index': 0, **call}]}, None), ({}, 'tool_calls')] if tool_request else [({'role': 'assistant', 'content': ''}, None), ({'content': 'E2E_GATEWAY_OK'}, None), ({}, 'stop')]
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
    from app.infra import extensions
    monkeypatch.setattr(extensions, 'redis_client', infrastructure['redis'])
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
    process: subprocess.Popen[str] | subprocess.Popen[bytes] | None = None
    boot_process: subprocess.Popen[str] | subprocess.Popen[bytes] | None = None
    permission_replay_run_ids: list[int] = []
    permission_http_responses: list[int] = []
    observed: list[Any] = []
    run: Any = None
    history: list[Any] = []
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
                            operations.record_candidate(bootstrap,old_generation,prepared["id"])
                            bootstrap.commit()
                            operations.switch_candidate(bootstrap,old_generation)
                            bootstrap.commit()
                    docker('rm','-f',prepared["id"])
                    from app.infra.runtime_operations import recover_runtime_operations
                    recover_runtime_operations(session_factory=factory)
                    with factory() as observer:
                        op = observer.get(RuntimeOperation,old_generation)
                        assert op is not None and op.state == 'cancelled'
                        replacement = observer.query(RuntimeOperation).filter_by(workspace_id=ws_id,user_id=user_id,state='complete').one()
                        assert replacement.generation != old_generation and replacement.candidate_container_id != prepared["id"]
                if crash_before_completion:
                    marker = tmp_path / 'configured-generation'
                    # 在外部配置及其 SQL 版本提交后、操作完成事务前
                    # 中断实际 locked-SDK/API 进程. 无容器 Mock.
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
                        assert boot_process.stdin is not None
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
                        assert pending is not None
                        assert pending.state == 'switched' and pending.finished_at is None
                        candidate_id = pending.candidate_container_id
                    from app.infra.runtime_operations import recover_runtime_operations
                    assert recover_runtime_operations(session_factory=factory) == 1
                    with factory() as observer:
                        recovered = observer.get(RuntimeOperation,generation)
                        assert recovered is not None
                        assert recovered.state == 'complete' and recovered.candidate_container_id == candidate_id
                with factory() as db:
                    db_ws = db.get(Workspace, ws_id)
                    assert db_ws is not None
                    workspace = db_ws
                    conversation = repository.create_conversation(
                        db,
                        repository.CreateConversationParams(
                            workspace_id=ws_id,
                            user_id=user_id,
                            mode='expert',
                            title='gateway task',
                        ),
                    )
                    conversation_id = int(conversation.id)
                    db.commit()
                    if permission_response != 'auth-version' and missing_candidate != 'permission-replay':
                        conversation, run, history = service.prepare_run(
                            db,
                            service.PrepareRunParams(
                                workspace=workspace,
                                user_id=user_id,
                                conversation_id=conversation.id,
                                query='只输出 E2E_GATEWAY_OK，不调用任何工具。',
                            ),
                        )
                    if missing_candidate == 'paused-prompt':
                        import signal
                        marker = tmp_path / 'paused-prompt'
                        child_code = """
import asyncio,json,sys,os,signal
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.features.assistant.service import StreamRunParams, stream_run
from app.features.assistant.clients.opencode_client import OpenCodeClient
settings=json.load(sys.stdin)
actual=OpenCodeClient.prompt_async
async def hold(self,*args,**kwargs):
    if not kwargs.get('no_reply'):
        Path(settings['marker']).write_text('ready')
        os.kill(os.getpid(),signal.SIGSTOP)
    return await actual(self,*args,**kwargs)
OpenCodeClient.prompt_async=hold
engine=create_engine(settings['database'])
async def run():
    async for event in stream_run(StreamRunParams(
        session_factory=sessionmaker(bind=engine),
        workspace_id=settings['workspace'],
        user_id=settings['user'],
        conversation_id=settings['conversation'],
        run_id=settings['run'],
        history=[],
    )):
        print(event.type,flush=True)
asyncio.run(run())
"""
                        with (tmp_path / 'paused-worker.log').open('w') as output:
                            boot_process = subprocess.Popen([sys.executable,'-c',child_code],env=environment,
                                stdin=subprocess.PIPE,stdout=output,stderr=subprocess.STDOUT,text=True)
                            assert boot_process.stdin is not None
                            boot_process.stdin.write(json.dumps({'database':postgres.url.render_as_string(hide_password=False),
                                'workspace':ws_id,'user':user_id,'conversation':conversation.id,'run':run.id,'marker':str(marker)}))
                            boot_process.stdin.close()
                            deadline = time.monotonic()+40
                            while not marker.exists():
                                assert boot_process.poll() is None
                                assert time.monotonic()<deadline
                                time.sleep(.05)
                            waited, stopped = os.waitpid(boot_process.pid,os.WUNTRACED)
                            assert waited == boot_process.pid and os.WIFSTOPPED(stopped)
                            from app.models.assistant import AssistantRun
                            from app.infra.assistant_abort import recover_abandoned_runs, abort_pending_sessions
                            from app.infra.datetime_utils import utc_now
                            from datetime import timedelta
                            with factory() as recovery:
                                abandoned = recovery.get(AssistantRun,run.id)
                                assert abandoned is not None
                                captured_session = abandoned.opencode_session_id
                                assert captured_session and not requests
                                abandoned.started_at = utc_now()-timedelta(seconds=60)
                                abandoned.metadata_dict = {**abandoned.metadata_dict,'worker_heartbeat_unix':time.time()-60}
                                recovery.commit()
                            def bounded_recovery_session():
                                recovery = factory()
                                recovery.execute(text("SET LOCAL lock_timeout = '8s'"))
                                return recovery
                            assert recover_abandoned_runs(session_factory=bounded_recovery_session)==1
                            assert abort_pending_sessions(session_factory=factory)==1
                            os.kill(boot_process.pid,signal.SIGCONT)
                            deadline=time.monotonic()+8
                            while boot_process.poll() is None and not requests and time.monotonic()<deadline:
                                time.sleep(.05)
                            assert not requests, 'recovered paused worker dispatched a new model request after remote cleanup'
                            assert boot_process.wait(5)==0
                        with factory() as recovery:
                            ar = recovery.get(AssistantRun,run.id)
                            assert ar is not None and ar.status == 'failed'
                            runtime = docker_service.runtime_service.get_runtime(recovery,ws_id,user_id)
                            assert runtime is not None
                            assert not infrastructure['redis'].exists(f'assistant:expert:runtime:{runtime.id}')
                        return
                    if missing_candidate in ('lease-before-sql','lease-set-reply'):
                        marker = tmp_path / 'lease-checkpoint'
                        lease_code = """
import asyncio,json,sys,time
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.features.assistant.service import StreamRunParams, stream_run
from app.features.assistant.engines.expert_engine import ExpertEngine
from app.infra import extensions
settings=json.load(sys.stdin)
if settings['mode']=='lease-before-sql':
    original=ExpertEngine._save_runtime_lease
    def hold(self,*args):
        Path(settings['marker']).write_text('before-sql')
        time.sleep(120)
        return original(self,*args)
    ExpertEngine._save_runtime_lease=hold
else:
    original=extensions.redis_client.set
    def hold(key,*args,**kwargs):
        result=original(key,*args,**kwargs)
        if key.startswith('assistant:expert:runtime:'):
            Path(settings['marker']).write_text('set-applied')
            time.sleep(120)
        return result
    extensions.redis_client.set=hold
engine=create_engine(settings['database'])
async def run():
    async for event in stream_run(StreamRunParams(
        session_factory=sessionmaker(bind=engine),
        workspace_id=settings['workspace'],
        user_id=settings['user'],
        conversation_id=settings['conversation'],
        run_id=settings['run'],
        history=[],
    )):
        print(event.type,flush=True)
asyncio.run(run())
"""
                        with (tmp_path / 'lease-worker.log').open('w') as output:
                            boot_process = subprocess.Popen([sys.executable,'-c',lease_code],env=environment,
                                stdin=subprocess.PIPE,stdout=output,stderr=subprocess.STDOUT,text=True)
                            assert boot_process.stdin is not None
                            boot_process.stdin.write(json.dumps({'database':postgres.url.render_as_string(hide_password=False),
                                'workspace':ws_id,'user':user_id,'conversation':conversation.id,'run':run.id,
                                'mode':missing_candidate,'marker':str(marker)}))
                            boot_process.stdin.close()
                            deadline = time.monotonic()+40
                            while not marker.exists():
                                assert boot_process.poll() is None, 'native worker exited before lease checkpoint'
                                assert time.monotonic()<deadline, 'native worker did not reach lease checkpoint'
                                time.sleep(.05)
                            boot_process.kill()
                            assert boot_process.wait(5)<0
                        from app.models.assistant import AssistantRun
                        from app.infra.assistant_abort import recover_abandoned_runs, abort_pending_sessions
                        from app.infra.datetime_utils import utc_now
                        from datetime import timedelta
                        from app.models.assistant_runtime_abort import AssistantRuntimeAbort
                        with factory() as observer:
                            runtime = docker_service.runtime_service.get_runtime(observer,ws_id,user_id)
                            assert runtime is not None
                            key = f'assistant:expert:runtime:{runtime.id}'
                            abandoned = observer.get(AssistantRun,run.id)
                            assert abandoned is not None
                            assert abandoned.opencode_session_id is None and not requests
                            if missing_candidate == 'lease-before-sql':
                                assert infrastructure['redis'].get(key) is None, 'Redis acquired before durable SQL ownership existed'
                                assert abandoned.runtime_lease_token is None
                            else:
                                assert abandoned.runtime_lease_token and infrastructure['redis'].get(key)==abandoned.runtime_lease_token, 'applied Redis SET has no recoverable SQL token'
                            abandoned.started_at = utc_now()-timedelta(seconds=60)
                            abandoned.metadata_dict = {**abandoned.metadata_dict,'worker_heartbeat_unix':time.time()-60}
                            observer.commit()
                        assert recover_abandoned_runs(session_factory=factory)==1
                        if missing_candidate == 'lease-set-reply':
                            with factory() as observer:
                                abort_record = observer.get(AssistantRuntimeAbort,run.id)
                                assert abort_record is not None and abort_record.session_id is None
                            assert abort_pending_sessions(session_factory=factory)==1
                        else:
                            assert abort_pending_sessions(session_factory=factory)==0
                        assert not infrastructure['redis'].exists(key)
                        return
                    if missing_candidate == 'command-crash':
                        # 在原生权限授予和实际 shell/子进程启动后
                        # 强杀实际流式 API worker.
                        stream_code = """
import asyncio,json,sys
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.features.assistant.service import StreamRunParams, stream_run
settings=json.load(sys.stdin)
engine=create_engine(settings['database'])
async def run():
    async for event in stream_run(StreamRunParams(
        session_factory=sessionmaker(bind=engine),
        workspace_id=settings['workspace'],
        user_id=settings['user'],
        conversation_id=settings['conversation'],
        run_id=settings['run'],
        history=[],
    )):
        print(event.type,flush=True)
asyncio.run(run())
"""
                        with (tmp_path / 'stream-worker.log').open('w') as stream_output:
                            boot_process = subprocess.Popen([sys.executable,'-c',stream_code],env=environment,
                                stdin=subprocess.PIPE,stdout=stream_output,stderr=subprocess.STDOUT,text=True)
                            assert boot_process.stdin is not None
                            boot_process.stdin.write(json.dumps({'database':postgres.url.render_as_string(hide_password=False),
                                'workspace':ws_id,'user':user_id,'conversation':conversation.id,'run':run.id}))
                            boot_process.stdin.close()
                            deadline = time.monotonic() + 40
                            from app.models.assistant import AssistantRun
                            while True:
                                assert boot_process.poll() is None, 'native streaming worker exited before permission request'
                                with factory() as responder:
                                    restored = responder.get(AssistantRun, run.id)
                                    assert restored is not None
                                    permission = restored.pending_permission_id
                                    if permission:
                                        resp_ws = responder.get(Workspace, ws_id)
                                        assert resp_ws is not None
                                        service.respond_permission(
                                            responder,
                                            service.RespondPermissionParams(
                                                workspace=resp_ws,
                                                user_id=user_id,
                                                run_id=run.id,
                                                permission_id=permission,
                                                response='once',
                                            ),
                                        )
                                        break
                                assert time.monotonic() < deadline, 'native worker never requested tool permission'
                                time.sleep(.05)
                            with factory() as reader:
                                runtime = docker_service.runtime_service.get_runtime(reader,ws_id,user_id)
                                assert runtime is not None and runtime.container_id is not None
                                container_id = runtime.container_id
                            while True:
                                assert boot_process.poll() is None, 'native worker exited before command started'
                                probe = docker('exec',container_id,'cat','/tmp/skycloud-command-pids',check=False)
                                if probe.returncode == 0:
                                    pids = probe.stdout.split()
                                    assert len(pids) == 2 and all(pid.isdecimal() for pid in pids)
                                    break
                                assert time.monotonic() < deadline, 'native granted command never started'
                                time.sleep(.05)
                            boot_process.kill()
                            assert boot_process.wait(5) < 0
                        # 显式老化持久化 SQL 时间以触发生产宽限谓词,
                        # 无需等待 45 秒.
                        from datetime import timedelta
                        from app.infra.datetime_utils import utc_now
                        with factory() as recovery:
                            abandoned = recovery.get(AssistantRun,run.id)
                            assert abandoned is not None
                            assert abandoned.status == 'running'
                            abandoned.started_at = utc_now() - timedelta(seconds=60)
                            abandoned.metadata_dict = {**abandoned.metadata_dict,'worker_heartbeat_unix':time.time()-60}
                            recovery.commit()
                        from app.infra.assistant_abort import abort_pending_sessions, recover_abandoned_runs
                        assert recover_abandoned_runs(session_factory=factory) == 1
                        from app.exceptions import ConflictError
                        from app.models.assistant_runtime_abort import AssistantRuntimeAbort
                        with factory() as recovery:
                            ar = recovery.get(AssistantRun, run.id)
                            assert ar is not None and ar.status == 'failed'
                            intent = recovery.get(AssistantRuntimeAbort, run.id)
                            assert intent is not None
                            assert intent.lease_token and infrastructure['redis'].get(f'assistant:expert:runtime:{intent.runtime_id}') == intent.lease_token
                            rec_ws = recovery.get(Workspace, ws_id)
                            assert rec_ws is not None
                            with pytest.raises(ConflictError, match='cleanup'):
                                service.prepare_run(
                                    recovery,
                                    service.PrepareRunParams(
                                        workspace=rec_ws,
                                        user_id=user_id,
                                        conversation_id=conversation.id,
                                        query='new task',
                                    ),
                                )
                        def unavailable(target: Any):
                            raise ConnectionError('synthetic-control-unavailable')
                        assert abort_pending_sessions(session_factory=factory, abort_function=unavailable) == 0
                        with factory() as recovery:
                            intent = recovery.get(AssistantRuntimeAbort, run.id)
                            assert intent is not None
                            assert intent.completed_at is None and intent.attempts == 1
                            intent.next_attempt_at = utc_now() - timedelta(seconds=1)
                            recovery.commit()
                        assert abort_pending_sessions(session_factory=factory) == 1
                        assert abort_pending_sessions(session_factory=factory) == 0
                        assert not infrastructure['redis'].exists(f'assistant:expert:runtime:{runtime.id}')
                        for pid in pids:
                            state = docker('exec',container_id,'cat',f'/proc/{pid}/stat',check=False)
                            if state.returncode == 0:
                                assert state.stdout.rsplit(')',1)[1].split()[0] == 'Z', 'abandoned native command still executing after SQL recovery'
                        assert docker('exec',container_id,'test','-e','/tmp/skycloud-command-late',check=False).returncode == 1
                        return
                    command_observers: list[asyncio.Task[Any]] = []
                    command_state: dict[str, Any] = {}
                    async def interrupt_started_command():
                        def current_container():
                            with factory() as reader:
                                crt = docker_service.runtime_service.get_runtime(reader, ws_id, user_id)
                                assert crt is not None and crt.container_id is not None
                                return crt.container_id
                        container_id = await asyncio.to_thread(current_container)
                        deadline = time.monotonic() + 10
                        while True:
                            probe = await asyncio.to_thread(docker, 'exec', container_id, 'cat', '/tmp/skycloud-command-pids', check=False)
                            if probe.returncode == 0:
                                pids = probe.stdout.split()
                                assert len(pids) == 2 and all(pid.isdecimal() for pid in pids)
                                command_state.update(container=container_id, pids=pids, started=time.monotonic())
                                break
                            assert time.monotonic() < deadline, 'granted native command never started'
                            await asyncio.sleep(.01)
                        if missing_candidate == 'command-lease-loss':
                            keys = list(infrastructure['redis'].scan_iter('assistant:expert:runtime:*'))
                            assert len(keys) == 1
                            replaced_lease_keys.extend(keys)
                            infrastructure['redis'].set(keys[0], 'successor-worker', ex=60)
                        else:
                            def cancel_run():
                                with factory() as writer:
                                    ws = writer.get(Workspace, ws_id)
                                    assert ws is not None
                                    service.request_cancel(writer, ws, user_id, run.id)
                            await asyncio.to_thread(cancel_run)
                    async def collect() -> list[Any]:
                        if permission_response == 'auth-version':
                            from app.features.assistant import router as assistant_router
                            from app.features.assistant.event_protocol import AssistantEvent
                            from app.features.auth.service import generate_token
                            from app.features.assistant.clients.opencode_client import OpenCodeClient
                            from app.models.assistant import AssistantRun
                            raw = generate_token(user_id, 0)
                            assert raw is not None
                            abort_acks: list[Any] = []
                            abort_closed: list[bool] = []
                            abort_errors: list[Any] = []
                            revoked: list[bool] = []
                            event_clients: list[Any] = []
                            actual_abort = OpenCodeClient.abort_session
                            actual_events = OpenCodeClient.stream_events
                            async def observed_events(client: Any):
                                event_clients.append(client._client)
                                async for event in actual_events(client):
                                    yield event
                            monkeypatch.setattr(OpenCodeClient, 'stream_events', observed_events)
                            async def observed_abort(client: Any, session_id: Any):
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
                            async def observed_app(scope: Any, receive: Any, send: Any):
                                async def observe(message: Any):
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
                            assert result is not None
                            assert result.status == 'cancelled' and result.pending_permission_id is None
                            assert observed[-1].type == 'cancelled'
                            assert service.run_registry.get(result.id) is None
                            with postgres.connect() as independent:
                                assert independent.scalar(text('SELECT auth_version FROM users WHERE id=:id'), {'id':user_id}) == 1
                                assert independent.scalar(text('SELECT role FROM workspace_members WHERE workspace_id=:ws AND user_id=:id'), {'ws':ws_id,'id':user_id}) == 'admin'
                            assert not any('AUTHORIZED_ONLY' in str(message.get('content','')) for body in requests for message in body['messages'] if message.get('role') == 'tool')
                            return observed
                        if missing_candidate == 'permission-replay':
                            from app.features.assistant import router as assistant_router
                            from app.features.assistant.event_protocol import AssistantEvent
                            from app.features.auth.service import generate_token
                            raw = generate_token(user_id, 0)
                            assert raw is not None
                            app = FastAPI()
                            app.include_router(assistant_router.router)
                            app.dependency_overrides[get_db] = lambda: None
                            app.dependency_overrides[get_session_factory] = lambda: factory

                            async def authenticated_app(scope: Any, receive: Any, send: Any):
                                async with httpx.AsyncClient(
                                    transport=httpx.ASGITransport(app=app),
                                    base_url='http://test',
                                ) as responder:
                                    async def observe(message: Any):
                                        await send(message)
                                        if message['type'] != 'http.response.body':
                                            return
                                        for line in message.get('body', b'').splitlines():
                                            if not line.startswith(b'data: '):
                                                continue
                                            payload = json.loads(line[6:])
                                            if payload.get('type') != 'permission_required':
                                                continue
                                            run_id = int(payload['run_id'])
                                            permission_id = payload['payload']['permission_id']
                                            result = await responder.post(
                                                f'/assistant/runs/{run_id}/permissions/{permission_id}',
                                                headers={
                                                    'Authorization': 'Bearer ' + raw,
                                                    'X-Workspace-Id': str(ws_id),
                                                },
                                                json={'response': 'once'},
                                            )
                                            permission_http_responses.append(result.status_code)
                                            assert result.status_code == 200, result.text

                                    await app(scope, receive, observe)

                            async with httpx.AsyncClient(
                                transport=httpx.ASGITransport(app=authenticated_app),
                                base_url='http://test',
                            ) as client:
                                response = await client.post(
                                    f'/assistant/conversations/{conversation_id}/messages/stream',
                                    headers={
                                        'Authorization': 'Bearer ' + raw,
                                        'X-Workspace-Id': str(ws_id),
                                    },
                                    json={'query': '只输出 E2E_GATEWAY_OK，不调用任何工具。'},
                                )
                            assert response.status_code == 200, response.text
                            assert raw not in response.text
                            permission_replay_run_ids.append(int(response.headers['X-Assistant-Run-Id']))
                            observed.extend(
                                AssistantEvent(**json.loads(line[6:]))
                                for line in response.text.splitlines()
                                if line.startswith('data: ')
                            )
                            return observed
                        assert run is not None
                        async for event in service.stream_run(
                            service.StreamRunParams(
                                session_factory=factory,
                                workspace_id=ws_id,
                                user_id=user_id,
                                conversation_id=conversation_id,
                                run_id=run.id,
                                history=history,
                            )):
                            observed.append(event)
                            if event.type == 'permission_required':
                                assert permission_response is not None
                                assert "printf 'AUTHORIZED_ONLY'" in event.payload['tool']
                                def respond_from_worker():
                                    with factory() as responder:
                                        from app.models.assistant import AssistantRun
                                        restored = responder.get(AssistantRun, run.id)
                                        assert restored is not None
                                        restored_dict = restored.to_dict()
                                        assert restored_dict['pending_permission'] is not None
                                        assert restored_dict['pending_permission']['tool'] == event.payload['tool']
                                        if permission_response in ('demote','remove'):
                                            responder.execute(text("UPDATE workspace_members SET role='viewer' WHERE workspace_id=:ws AND user_id=:user" if permission_response == 'demote' else "DELETE FROM workspace_members WHERE workspace_id=:ws AND user_id=:user"),{'ws':ws_id,'user':user_id})
                                            responder.commit()
                                        else:
                                            resp_ws = responder.get(Workspace, ws_id)
                                            assert resp_ws is not None
                                            service.respond_permission(
                                                responder,
                                                service.RespondPermissionParams(
                                                    workspace=resp_ws,
                                                    user_id=user_id,
                                                    run_id=run.id,
                                                    permission_id=(
                                                        event.payload[
                                                            'permission_id']
                                                    ),
                                                    response=(
                                                        'once'
                                                        if permission_response
                                                        == 'unconfirmed'
                                                        else permission_response
                                                    ),
                                                ),
                                            )
                                await asyncio.to_thread(respond_from_worker)
                                if missing_candidate in ('command-lease-loss','command-cancel','command-cancel-retry'):
                                    command_observers.append(asyncio.create_task(interrupt_started_command()))
                        if command_observers:
                            await asyncio.gather(*command_observers)
                        return observed
                    events: list[Any] = asyncio.run(asyncio.wait_for(collect(), 120))
                    if permission_response == 'auth-version':
                        return
                    if missing_candidate == 'permission-replay':
                        from app.models.assistant import AssistantPermission, AssistantRun
                        assert len(permission_replay_run_ids) == 1
                        assert sum(event.type == 'permission_required' for event in events) == 1
                        assert permission_http_responses == [200]
                        assert len(permission_dispatches) == 1
                        with factory() as replay_session:
                            replay_run = replay_session.get(
                                AssistantRun, permission_replay_run_ids[0],
                            )
                            assert replay_run is not None
                            assert replay_run.status == 'completed'
                            permissions = replay_session.query(
                                AssistantPermission,
                            ).filter(
                                AssistantPermission.run_id == replay_run.id,
                            ).all()
                            assert len(permissions) == 1
                            replayed_permission = permissions[0]
                            assert replayed_permission.state == 'dispatched'
                            assert replayed_permission.response == 'once'
                            assert replayed_permission.answered_at is not None
                            assert replayed_permission.dispatched_at is not None
                        return
                    assert run is not None
                    db.refresh(run)
                    if missing_candidate in ('command-lease-loss','command-cancel','command-cancel-retry'):
                        assert command_state, 'command cancellation boundary was not exercised'
                        assert run.status in ('failed','cancelled')
                        assert not any(event.type == 'done' for event in events)
                        if missing_candidate == 'command-cancel-retry':
                            from app.infra.assistant_abort import abort_pending_sessions
                            from app.models.assistant_runtime_abort import AssistantRuntimeAbort
                            from app.infra.datetime_utils import utc_now
                            from datetime import timedelta
                            from app.features.assistant.clients.opencode_client import OpenCodeClient
                            with factory() as reader:
                                intent = reader.get(AssistantRuntimeAbort,run.id)
                                assert intent is not None and intent.completed_at is None
                                assert intent.session_id == run.opencode_session_id
                                from app.exceptions import ConflictError
                                read_ws = reader.get(Workspace, ws_id)
                                assert read_ws is not None
                                with pytest.raises(ConflictError, match='cleanup'):
                                    service.prepare_run(
                                        reader,
                                        service.PrepareRunParams(
                                            workspace=read_ws,
                                            user_id=user_id,
                                            conversation_id=conversation.id,
                                            query='new work',
                                        ),
                                    )
                            probe = docker('exec',command_state['container'],'cat',f"/proc/{command_state['pids'][0]}/stat",check=False)
                            assert probe.returncode == 0 and probe.stdout.rsplit(')',1)[1].split()[0] != 'Z', 'control outage did not leave a running command to recover'
                            assert abort_pending_sessions(session_factory=factory)==0
                            with factory() as writer:
                                intent = writer.get(AssistantRuntimeAbort,run.id)
                                assert intent is not None
                                assert intent.attempts == 1 and intent.completed_at is None
                                intent.next_attempt_at = utc_now()-timedelta(seconds=1)
                                writer.commit()
                            monkeypatch.setattr(OpenCodeClient,'abort_session',actual_abort)
                            assert abort_pending_sessions(session_factory=factory)==1
                            assert abort_pending_sessions(session_factory=factory)==0
                        else:
                            assert lease_abort_acks and all(lease_abort_acks)
                        for pid in command_state['pids']:
                            result = docker('exec', command_state['container'], 'cat', f'/proc/{pid}/stat', check=False)
                            if result.returncode == 0:
                                assert result.stdout.rsplit(')', 1)[1].split()[0] == 'Z', 'granted command process still executing after acknowledged abort'
                        if missing_candidate != 'command-cancel-retry':
                            time.sleep(max(0, command_state['started'] + 4.5 - time.monotonic()))
                        assert docker('exec', command_state['container'], 'test', '-e', '/tmp/skycloud-command-late', check=False).returncode == 1
                        assert len(requests) == 1, 'aborted command triggered a later model call'
                        runtime = docker_service.runtime_service.get_runtime(db, ws_id, user_id)
                        assert runtime is not None
                        if missing_candidate == 'command-lease-loss':
                            assert infrastructure['redis'].get(replaced_lease_keys[0]) == 'successor-worker'
                        else:
                            assert not infrastructure['redis'].exists(f'assistant:expert:runtime:{runtime.id}')
                        return
                    if missing_candidate == 'lease-loss':
                        assert run.status in ('failed', 'cancelled')
                        assert not any(event.type == 'done' for event in events)
                        assert not any(event.type == 'token' for event in events)
                        assert lease_abort_acks and all(lease_abort_acks)
                        assert provider_disconnected.wait(5), 'provider HTTP remained open after acknowledged OpenCode abort'
                        assert len(replaced_lease_keys) == 1
                        assert infrastructure['redis'].get(replaced_lease_keys[0]) == 'successor-worker'
                        assert repository.get_messages(db, conversation.id)[-1].content == ''
                        return
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
                        assert current_runtime is not None
                        async def failure_messages():
                            assert current_runtime is not None
                            async with OpenCodeClient(docker_service.get_runtime_base_url(workspace,user_id,current_runtime),server_username(),server_password(current_runtime.id,user_id,ws_id)) as failure_client:
                                return await failure_client.get_messages(run.opencode_session_id)
                        failures = asyncio.run(failure_messages())
                        raw_failures: list[dict[str, Any]] = cast(list[dict[str, Any]], failures)
                        import re
                        detail = json.dumps([{key: cast(dict[str, Any], message.get('info', {})).get(key) for key in ('id','role','parentID','time','finish','error')} for message in raw_failures])
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
                        async def refuse_prompt(client: Any,method: Any,path: Any,**kwargs: Any):
                            if path.endswith('/prompt_async'):
                                raise OpenCodeClientError('OpenCode returned HTTP 503',503)
                            return await original_json(client,method,path,**kwargs)
                        monkeypatch.setattr(OpenCodeClient,'_json',refuse_prompt)
                        # 重复同一问题: 内容相等性无法将旧回答与此
                        # 被拒绝的尝试区分开.
                        conversation, run, history = service.prepare_run(
                            db,
                            service.PrepareRunParams(
                                workspace=workspace,
                                user_id=user_id,
                                conversation_id=conversation.id,
                                query='只输出 E2E_GATEWAY_OK，不调用任何工具。',
                            ),
                        )
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
                        assert first_runtime is not None
                        first_container = first_runtime.container_id
                        assert first_container is not None
                        first_generation = db.query(RuntimeOperation).filter_by(state='complete').one().generation
                        docker('rm','-f',first_container)  # Lose actual OpenCode session storage.
                        request_count = len(requests)
                        conversation, run, history = service.prepare_run(
                            db,
                            service.PrepareRunParams(
                                workspace=workspace,
                                user_id=user_id,
                                conversation_id=conversation.id,
                                query='续接历史，只输出 E2E_GATEWAY_OK，不调用工具。',
                            ),
                        )
                        assert len(history) == 2
                        observed.clear()
                        events = asyncio.run(asyncio.wait_for(collect(),120))
                        db.refresh(run); db.refresh(conversation)
                        assert run.status == 'completed', [(event.type,event.payload) for event in events]
                        assert conversation.opencode_session_id == run.opencode_session_id != first_session
                        assert len(repository.get_messages(db,conversation.id)) == 4
                        assert 'E2E_GATEWAY_OK' in repository.get_messages(db,conversation.id)[-1].content
                        # 新提供商会话在第二个用户提示词前通过实际网关
                        # 接收已提交的 SQL 历史.
                        replayed = json.dumps([body['messages'] for body in requests[request_count:]],ensure_ascii=False)
                        assert '只输出 E2E_GATEWAY_OK，不调用任何工具。' in replayed
                        assert '续接历史' in replayed
                        db.expire_all()
                        restored_runtime = docker_service.runtime_service.get_runtime(db,ws_id,user_id)
                        assert restored_runtime is not None
                        assert restored_runtime.container_id != first_container
                        first_op = db.get(RuntimeOperation,first_generation)
                        assert first_op is not None and first_op.state == 'cancelled'

                    runtime = docker_service.runtime_service.get_runtime(db, ws_id, user_id)
                    assert runtime is not None
                    assert not infrastructure['redis'].exists(f'assistant:expert:runtime:{runtime.id}')
                    operation = db.query(RuntimeOperation).filter_by(workspace_id=ws_id,user_id=user_id,state='complete').one()
                    assert operation.state == 'complete' and operation.finished_at is not None
                    assert operation.candidate_container_id == runtime.container_id
                    runtime_config: Any = docker_service._client().read_config(str(runtime.container_id))
                    assert provider_key not in json.dumps(runtime_config)
                    raw_api_key: str = str(runtime_config['provider'][docker_service.OPENCODE_CHAT_PROVIDER_ID]['options']['apiKey'])
                    from app.features.runtime_llm import tokens
                    token_info = tokens.validate_token(db, raw_api_key)
                    assert token_info is not None and token_info['type'] == 'runtime_llm'
                    assert list(infrastructure['redis'].scan_iter('runtime-llm-quota:*:requests'))
                    # 通过独立 worker Session 以及冷启动执行
                    # 现有运行时凭据刷新.
                    from app.features.assistant.engines.expert_engine import ExpertEngine
                    async def refresh_existing():
                        engine = ExpertEngine(session=db, conversation=conversation, run=run)
                        return [item async for item in engine._ensure_runtime(ws_id, user_id)]
                    refreshed = asyncio.run(refresh_existing())
                    assert refreshed[0][1].payload['runtime_status'] == 'running'
                    rt_res = refreshed[-1][0]
                    assert rt_res is not None and rt_res.runtime_id == runtime.id
                    from app.features.assistant.clients.opencode_client import OpenCodeClient
                    from app.features.assistant.clients.opencode_auth import server_password, server_username
                    async def mcp_status():
                        async with OpenCodeClient(docker_service.get_runtime_base_url(workspace, user_id, runtime), server_username(), server_password(runtime.id, user_id, ws_id)) as client:
                            return await client.get_mcp_status(), await client.get_messages(run.opencode_session_id)
                    connected, messages = asyncio.run(mcp_status())
                    connected_dict: Any = connected
                    assert connected_dict['SKYCLOUD']['status'] == 'connected'
                    # 在热刷新后使用最新限定范围 Runtime 凭据
                    # 通过其协议调用实际 MCP 搜索工具.
                    mcp_headers: Any = runtime_config['mcp']['SKYCLOUD']['headers']
                    # 父级单元 conftest 替换了 MCP 包. 像服务器一样
                    # 在独立进程中运行锁定 SDK.
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
                    # 针对实际持久化的专家结果校验公开 DTO.
                    from app.features.assistant.schemas import (
                        AssistantConversationResponse, AssistantRunResponse, AssistantMessageResponse)
                    AssistantConversationResponse.model_validate(conversation.to_dict())
                    AssistantRunResponse.model_validate(run.to_dict())
                    for stored_message in repository.get_messages(db, conversation.id):
                        AssistantMessageResponse.model_validate(stored_message.to_dict())
                    if permission_response == 'reject':
                        raw_messages: list[dict[str, Any]] = cast(list[dict[str, Any]], messages)
                        states: list[dict[str, Any]] = [
                            part['state']
                            for message in raw_messages
                            for part in [cast(dict[str, Any], p) for p in cast(list[Any], message.get('parts', [])) if isinstance(p, dict)]
                            if part.get('type') == 'tool' and part.get('tool') == 'bash'
                        ]
                        assert any(state.get('status') == 'error' and 'rejected' in str(state.get('error', '')).lower() for state in states)
                        assert all('AUTHORIZED_ONLY' not in str(state.get('output', '')) for state in states)

    finally:
        # 新启动名称为持久化代系. 在 fixture 清理中保留每个
        # 已准入名称, 包括候选 SQL 前的失败.
        with factory() as cleanup_db:
            generations = cleanup_db.query(RuntimeOperation.generation).filter_by(workspace_id=ws_id,user_id=user_id).all()
            for (generation,) in generations:
                infrastructure['cleanup'].append(f'skycloud-opencode-w{ws_id}-u{user_id}-g{generation}')
            current = docker_service.runtime_service.get_runtime(cleanup_db,ws_id,user_id)
            if current and current.container_id:
                runtime_name = current.container_id
        (FIXTURE_DIR / 'skycloud-expert-gateway-results.json').write_text(json.dumps([{'fields': list(body), 'max_tokens': body.get('max_tokens')} for body in requests]))
        (FIXTURE_DIR / 'skycloud-expert-gateway-events.json').write_text(json.dumps([{'type': getattr(event, 'type', ''), 'payload': getattr(event, 'payload', {})} for event in observed], ensure_ascii=False))
        for name, label in [(runtime_name, 'runtime'), (bridge, 'proxy')]:
            logs = docker('logs', name, check=False)
            (FIXTURE_DIR / f'skycloud-expert-gateway-{label}.log').write_text(logs.stdout + logs.stderr)
        gateway.dependency_overrides.pop(get_db, None)
        if boot_process is not None and boot_process.poll() is None:
            boot_process.kill(); boot_process.wait(5)
        if process is not None:
            process.terminate()
            try: process.wait(10)
            except subprocess.TimeoutExpired: process.kill(); process.wait(5)


def test_mcp_upload_is_scoped_over_streamable_http(
    infrastructure: Any, postgres: Any, minio_storage: Any, tmp_path: Any,
) -> None:
    """通过真实 MCP 协议测试上传、回读与角色隔离."""
    upgrade(postgres)
    factory = sessionmaker(bind=postgres)
    with factory.begin() as db:
        writer = User(
            username='mcp-upload-writer-' + uuid.uuid4().hex[:8],
            password_hash='synthetic-unused-hash',
            role='common',
        )
        viewer = User(
            username='mcp-upload-viewer-' + uuid.uuid4().hex[:8],
            password_hash='synthetic-unused-hash',
            role='common',
        )
        db.add_all([writer, viewer])
        db.flush()
        first = Workspace(name='MCP upload first', owner_id=writer.id)
        second = Workspace(name='MCP upload second', owner_id=writer.id)
        db.add_all([first, second])
        db.flush()
        db.add_all([
            WorkspaceMember(
                workspace_id=first.id, user_id=writer.id, role='admin',
            ),
            WorkspaceMember(
                workspace_id=second.id, user_id=writer.id, role='admin',
            ),
            WorkspaceMember(
                workspace_id=first.id, user_id=viewer.id, role='viewer',
            ),
        ])
        db.flush()
        for workspace_id in (first.id, second.id):
            db.execute(
                text(
                    "INSERT INTO folder (name, workspace_id, parent_id) "
                    "VALUES ('/', :workspace_id, NULL)",
                ),
                {'workspace_id': workspace_id},
            )
        writer_id, viewer_id = writer.id, viewer.id
        first_id, second_id = first.id, second.id

    from app.features.auth.service import generate_token

    writer_token = generate_token(writer_id)
    viewer_token = generate_token(viewer_id)
    assert writer_token and viewer_token
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        mcp_port = probe.getsockname()[1]
    environment = {
        **os.environ,
        'DATABASE_URL': postgres.url.render_as_string(hide_password=False),
        'REDIS_HOST': '127.0.0.1',
        'REDIS_PORT': str(
            infrastructure['redis'].connection_pool.connection_kwargs['port'],
        ),
        'MCP_PORT': str(mcp_port),
        'MINIO_ENDPOINT': minio_storage.integration_endpoint,
        'MINIO_ACCESS_KEY': minio_storage.integration_auth[0],
        'MINIO_SECRET_KEY': minio_storage.integration_auth[1],
        'MINIO_BUCKET': minio_storage.integration_bucket,
        'MINIO_SECURE': 'false',
        'UPLOAD_FOLDER': str(tmp_path / 'mcp-upload'),
    }
    server_url = f'http://127.0.0.1:{mcp_port}/mcp'
    log_path = tmp_path / 'mcp-upload-server.log'
    with log_path.open('w') as server_log:
        process = subprocess.Popen(
            [sys.executable, '-m', 'entry.mcp_run'],
            cwd=Path(__file__).resolve().parents[2],
            env=environment,
            stdout=server_log,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                assert process.poll() is None, log_path.read_text()[-4000:]
                try:
                    if httpx.get(
                        server_url, timeout=1, trust_env=False,
                    ).status_code == 406:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            else:
                raise AssertionError(
                    'MCP upload subprocess did not start: '
                    + log_path.read_text()[-4000:],
                )

            client_code = """
import asyncio, json, sys, httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

settings = json.load(sys.stdin)
content = 'mcp-upload-proof-你好'
filename = 'mcp-upload-proof.txt'
forbidden = 'mcp-viewer-upload-forbidden.txt'

def text_of(result):
    values = [item.text for item in result.content if getattr(item, 'text', None)]
    assert values, repr(result)
    return values[0]

def json_of(result):
    value = json.loads(text_of(result))
    assert isinstance(value, dict), value
    return value

async def in_session(workspace_id, token, operation):
    headers = {
        'Authorization': 'Bearer ' + token,
        'X-SKYCloud-Workspace-Id': str(workspace_id),
    }
    async with httpx.AsyncClient(
        headers=headers, trust_env=False, timeout=20,
    ) as client:
        async with streamable_http_client(
            settings['url'], http_client=client,
        ) as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = {tool.name for tool in tools.tools}
                assert {'upload_file', 'list_files', 'read_file_content'} <= names
                return await operation(session)

async def main():
    async def publish_and_read(session):
        uploaded = await session.call_tool('upload_file', {
            'filename': filename,
            'content': content,
            'content_encoding': 'utf-8',
        })
        assert not uploaded.isError, text_of(uploaded)
        publication = json_of(uploaded)
        assert publication['success'] is True, publication
        file = publication['file']
        file_id = int(file['id'])
        listing_result = await session.call_tool('list_files', {})
        assert not listing_result.isError, text_of(listing_result)
        listing = json_of(listing_result)
        assert any(
            int(item['id']) == file_id and item['name'] == filename
            for item in listing['files']['items']
        ), listing
        read_result = await session.call_tool(
            'read_file_content', {'file_id': file_id},
        )
        assert not read_result.isError, text_of(read_result)
        readback = json_of(read_result)
        assert readback['content'] == content, readback
        assert readback['file_size'] == 23, readback
        assert readback['encoding'] == 'utf-8', readback
        assert readback['truncated'] is False, readback
        return file_id, readback

    file_id, readback = await in_session(
        settings['first_workspace'], settings['writer_token'],
        publish_and_read,
    )

    async def reject_cross_workspace_read(session):
        result = await session.call_tool(
            'read_file_content', {'file_id': file_id},
        )
        payload = json_of(result)
        assert payload.get('error'), payload
        assert 'content' not in payload, payload
        return {'isError': result.isError, 'error': payload['error']}

    workspace_error = await in_session(
        settings['second_workspace'], settings['writer_token'],
        reject_cross_workspace_read,
    )

    async def reject_viewer_upload(session):
        result = await session.call_tool('upload_file', {
            'filename': forbidden,
            'content': 'must-not-publish',
            'content_encoding': 'utf-8',
        })
        text = text_of(result)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = {'error': text}
        assert result.isError or payload.get('error'), payload
        assert payload.get('error'), payload
        return {'isError': result.isError, 'error': payload['error']}

    viewer_error = await in_session(
        settings['first_workspace'], settings['viewer_token'],
        reject_viewer_upload,
    )

    async def verify_no_forbidden_file(session):
        result = await session.call_tool('list_files', {})
        assert not result.isError, text_of(result)
        listing = json_of(result)
        names = {
            item['name'] for item in listing['files']['items']
        }
        assert filename in names, names
        assert forbidden not in names, names
        return sorted(names)

    visible = await in_session(
        settings['first_workspace'], settings['writer_token'],
        verify_no_forbidden_file,
    )
    print(json.dumps({
        'file_id': file_id,
        'readback': readback,
        'workspace_error': workspace_error,
        'viewer_error': viewer_error,
        'visible': visible,
    }, ensure_ascii=False))

asyncio.run(main())
"""
            checked = subprocess.run(
                [sys.executable, '-c', client_code],
                input=json.dumps({
                    'url': server_url,
                    'first_workspace': first_id,
                    'second_workspace': second_id,
                    'writer_token': writer_token,
                    'viewer_token': viewer_token,
                }),
                capture_output=True,
                text=True,
                timeout=60,
            )
            assert checked.returncode == 0, (
                checked.stderr[-4000:] + checked.stdout[-4000:]
                + log_path.read_text()[-4000:]
            )
            result = json.loads(checked.stdout)
            assert result['readback']['content'] == 'mcp-upload-proof-你好'
            assert result['readback']['file_size'] == 23
            assert result['readback']['truncated'] is False
            assert result['workspace_error']['error']
            assert result['viewer_error']['error']
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(5)
