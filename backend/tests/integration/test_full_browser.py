from collections.abc import Callable
from typing import Any, cast
# pyright: reportPrivateUsage=false
"""针对实际前端/nginx、API、PG、Redis 和 MinIO 的手动 CUA 验证."""
import asyncio
import json
import os
from contextlib import contextmanager
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import uuid

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from tests.integration.fixture_paths import FIXTURE_DIR
from tests.integration.test_runtime_security import infrastructure, controller, docker
from tests.integration.test_migrations import postgres
from tests.integration.test_upload_storage import minio_storage

__all__ = ["infrastructure", "controller", "docker", "postgres", "minio_storage"]
from entry.migrate import upgrade
from app.features.auth import user_service
from app.infra.schema import check_schema
from app.models.folder import Folder
from app.models.opencode_runtime import OpenCodeRuntime
from app.models.runtime_operation import RuntimeOperation
from app.models.token_usage_log import TokenUsageLog
from app.models.workspace import Workspace, WorkspaceMember

pytestmark = pytest.mark.skipif(os.getenv('RUN_FULL_BROWSER') != '1', reason='Opt-in manual full browser verification')

# 固定合成提供商标识: 仅替换外部供应商.
PROVIDER_KEY = 'browser-fixture-model-key'
CHAT_MODEL = 'browser-fixture-chat'
EMB_MODEL = 'browser-fixture-embedding'
PROVIDER_USAGE = {'prompt_tokens': 40, 'completion_tokens': 10, 'total_tokens': 50}
BROWSER_PARTIAL = 'BROWSER_PARTIAL'
BROWSER_STREAM_DONE = 'BROWSER_STREAM_DONE'
REWRITE_KEYWORDS = {
    'topic_terms': ['浏览器验证'],
    'entity_terms': ['合成供应商'],
    'time_terms': [],
    'file_type_terms': [],
    'action_terms': ['断言'],
    'synonym_terms': ['browser fixture'],
}
EMBEDDING_VECTOR = [1.0] + [0.0] * 1023
USAGE_FAULT_LOCATIONS = '''location = /api/token-usage/logs {
    return 502;
}

location = /api/admin/token-usage/logs {
    return 502;
}
'''
# 账户、操作、模型、提示词、补全、总量
USAGE_SAMPLES = (
    ('normal', 'chat', CHAT_MODEL, 100, 50, 150),
    ('normal', 'embedding', EMB_MODEL, 20, 0, 20),
    ('admin', 'chat', CHAT_MODEL, 30, 10, 40),
)


def free_port(host: str) -> int:
    with socket.socket() as probe:
        probe.bind((host, 0))
        return probe.getsockname()[1]


@contextmanager
def serve(app: Any):
    sock = socket.socket(); sock.bind(('127.0.0.1', 0)); sock.listen(128)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level='warning', access_log=False))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True); thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started, 'Synthetic model provider did not start'
        yield port
    finally:
        server.should_exit = True; thread.join(10); sock.close()
        assert not thread.is_alive(), 'Synthetic model provider thread did not stop'


def _sse(payload: Any):
    return 'data: ' + json.dumps(payload) + '\n\n'


def _completion(content: Any):
    return {'id': 'chatcmpl-browser-fixture', 'object': 'chat.completion', 'created': int(time.time()),
            'model': CHAT_MODEL,
            'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': content}, 'finish_reason': 'stop'}],
            'usage': PROVIDER_USAGE}


def build_provider():
    """带测试控制流屏障的合成兼容 OpenAI 供应商."""
    released, closing = threading.Event(), threading.Event()
    fault: dict[str, Any] = {'apply': None}
    provider = FastAPI()

    @provider.get('/fixture/release')
    @provider.post('/fixture/release')
    def release_answer():
        released.set()
        return {'released': True}

    @provider.get('/fixture/usage-fault')
    @provider.post('/fixture/usage-fault')
    def usage_fault(enabled: int):
        apply = fault['apply']
        if apply is None:
            return JSONResponse({'error': 'fixture nginx is not running'}, status_code=503)
        apply(bool(enabled))
        return JSONResponse({'fault': bool(enabled)})

    @provider.post('/v1/chat/completions')
    async def chat_completions(request: Request):
        body = await request.json()
        assert request.headers.get('authorization') == 'Bearer ' + PROVIDER_KEY
        assert body['model'] == CHAT_MODEL
        if body.get('response_format', {}).get('type') == 'json_schema':
            # 重写请求可能通过 LangChain 事件流水线流式传输.
            content = json.dumps(REWRITE_KEYWORDS)
            if not body.get('stream'):
                return JSONResponse(_completion(content))
            async def structured_frames():
                base = {'id': 'chatcmpl-browser-fixture', 'object': 'chat.completion.chunk',
                        'created': int(time.time()), 'model': CHAT_MODEL}
                yield _sse({**base, 'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': ''}, 'finish_reason': None}]})
                yield _sse({**base, 'choices': [{'index': 0, 'delta': {'content': content}, 'finish_reason': None}]})
                yield _sse({**base, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]})
                stream_options = body.get('stream_options')
                if isinstance(stream_options, dict) and cast(dict[str, Any], stream_options).get('include_usage'):
                    yield _sse({**base, 'choices': [], 'usage': PROVIDER_USAGE})
                yield 'data: [DONE]\n\n'
            return StreamingResponse(structured_frames(), media_type='text/event-stream')
        if not body.get('stream'):
            return JSONResponse(_completion(BROWSER_PARTIAL + BROWSER_STREAM_DONE))
        async def frames():
            base = {'id': 'chatcmpl-browser-fixture', 'object': 'chat.completion.chunk',
                    'created': int(time.time()), 'model': CHAT_MODEL}
            yield _sse({**base, 'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': ''}, 'finish_reason': None}]})
            yield _sse({**base, 'choices': [{'index': 0, 'delta': {'content': BROWSER_PARTIAL}, 'finish_reason': None}]})
            while not (released.is_set() or closing.is_set()):
                await asyncio.sleep(0.05)
            yield _sse({**base, 'choices': [{'index': 0, 'delta': {'content': BROWSER_STREAM_DONE}, 'finish_reason': None}]})
            yield _sse({**base, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]})
            yield _sse({**base, 'choices': [], 'usage': PROVIDER_USAGE})
            yield 'data: [DONE]\n\n'
        return StreamingResponse(frames(), media_type='text/event-stream')

    @provider.post('/v1/embeddings')
    async def embeddings(request: Request):
        body = await request.json()
        assert request.headers.get('authorization') == 'Bearer ' + PROVIDER_KEY
        assert body['model'] == EMB_MODEL
        inputs = body['input'] if isinstance(body['input'], list) else [body['input']]
        return JSONResponse({
            'object': 'list', 'model': EMB_MODEL,
            'data': [{'object': 'embedding', 'index': index, 'embedding': EMBEDDING_VECTOR}
                     for index in range(len(inputs))],
            'usage': {'prompt_tokens': 7 * len(inputs), 'total_tokens': 7 * len(inputs)},
        })

    return provider, released, closing, fault


def test_actual_frontend_auth_and_runtime(infrastructure: Any, controller: Any, postgres: Any, minio_storage: Any, tmp_path: Any) -> None:
    from app.features.workspace import docker_service
    root = Path(__file__).resolve().parents[3]
    backend = Path(__file__).resolve().parents[2]
    assert (root / 'frontend/dist/index.html').is_file(), 'Build the production frontend first'
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    nginx_image = next(line.split()[1] for line in (root/'frontend/Dockerfile').read_text().splitlines()
                       if line.startswith('FROM nginx:'))
    assert '@sha256:' in nginx_image, 'Browser fixture must use pinned production nginx'
    runtime_image = os.getenv('SKYCLOUD_TEST_OPENCODE_IMAGE') or os.getenv('SKYCLOUD_TEST_RUNTIME_IMAGE', '')
    assert runtime_image, ('Build scripts/build-runtime-image.py and set SKYCLOUD_TEST_OPENCODE_IMAGE '
                           'to its immutable ID (or SKYCLOUD_TEST_RUNTIME_IMAGE when only the stand-in is needed)')
    assert minio_storage.integration_bucket, 'minio_storage fixture must expose integration_bucket'
    upgrade(postgres); check_schema(postgres)
    factory = sessionmaker(bind=postgres)
    username, password = 'browser-' + uuid.uuid4().hex[:12], 'disposable-browser-password-32'
    admin_username = 'browser-admin-' + uuid.uuid4().hex[:12]
    admin_password = 'disposable-browser-admin-password-32'
    with factory() as db:
        for table in ('users', 'workspaces', 'opencode_runtimes'):
            db.execute(text("SELECT setval(pg_get_serial_sequence(:table, 'id'), :value, false)"),
                       {'table': table, 'value': int(uuid.uuid4().hex[:7], 16) + 1})
        user = user_service.create_user(db, {'username': username, 'password': password})
        second = Workspace(name=username + ' second space', owner_id=user.id)
        db.add(second); db.flush()
        db.add_all([
            WorkspaceMember(workspace_id=second.id, user_id=user.id, role='admin'),
            Folder(name='/', workspace_id=second.id, parent_id=None),
        ])
        admin = user_service.create_user(db, {'username': admin_username, 'password': admin_password})
        # 管理员角色是在此事务内准备的一次性 fixture 数据;
        # 生产产品不保留仅限 fixture 的提权路径.
        admin.role = 'admin'
        accounts = {'normal': user.id, 'admin': admin.id}
        db.add_all([
            TokenUsageLog(user_id=accounts[account], action=action, model_name=model,
                          prompt_tokens=prompt, completion_tokens=completion, total_tokens=total,
                          query_summary='browser fixture usage sample')
            for account, action, model, prompt, completion, total in USAGE_SAMPLES
        ])
        db.commit()
        user_id = int(user.id)
        workspace = db.query(Workspace).filter_by(owner_id=user.id).order_by(Workspace.id).first()
        assert workspace is not None and workspace.id is not None
        assert second is not None and second.id is not None
        workspace_ids = [workspace.id, second.id]
        runtime = OpenCodeRuntime(workspace_id=workspace.id, user_id=user_id, status='stopped')
        db.add(runtime); db.commit()
        name = f'skycloud-opencode-w{workspace.id}-u{user_id}'
        assert docker('inspect', name, check=False).returncode != 0
        infrastructure['cleanup'].append(name)
        runtime_id = runtime.id
        try:
            docker_service.start(db, workspace, user_id)
        finally:
            with factory() as cleanup_db:
                for generation, candidate in cleanup_db.query(RuntimeOperation.generation,
                        RuntimeOperation.candidate_container_id).filter_by(workspace_id=workspace.id, user_id=user_id).all():
                    for identity in (f'skycloud-opencode-w{workspace.id}-u{user_id}-g{generation}', candidate):
                        if identity and identity not in infrastructure['cleanup']:
                            infrastructure['cleanup'].append(identity)
        db.expire_all()
        running_runtime = db.get(OpenCodeRuntime, runtime_id)
        assert running_runtime is not None
        operation = db.query(RuntimeOperation).filter_by(runtime_id=runtime_id).one()
        assert running_runtime.status == 'running' and running_runtime.config_version > 0
        assert operation.state == 'complete' and operation.candidate_container_id == running_runtime.container_id
    bind_host = '127.0.0.1'
    if sys.platform.startswith('linux'):
        # Docker Desktop 将 host.docker.internal 转发至主机环回口. Linux
        # 引擎将其映射到桥接网关地址, 导致仅环回口的 API 不可达,
        # 因此在基础设施网络的网关上发布.
        # Docker 的 host-gateway 别名是默认网桥网关 (通常为 172.17.0.1),
        # 因此绑定在此处, 而非 fixture 网络的网关.
        bind_host = docker('network', 'inspect', 'bridge', '--format',
                           '{{(index .IPAM.Config 0).Gateway}}').stdout.strip()
        assert bind_host, 'Default bridge network has no gateway address'
    api_port = free_port(bind_host)
    parent_port, ui_port = free_port('127.0.0.1'), free_port('127.0.0.1')
    parent = f'http://app.skycloud.localhost:{parent_port}'
    metadata = FIXTURE_DIR / 'skycloud-full-browser.json'
    completion = tmp_path / 'browser-completed'
    frontend = infrastructure['target'] + '-frontend'
    infrastructure['cleanup'].append(frontend)
    fault_include = tmp_path / 'usage-fault.conf'
    fault_include.write_text('')
    config = tmp_path / 'default.conf.template'
    api_process = None
    provider, released, closing, fault = build_provider()
    redis_port = infrastructure['redis'].connection_pool.connection_kwargs['port']
    with serve(provider) as provider_port:
        try:
            # 子 API 进程绝不导入测试 conftest: 显式传递每个 fixture 持有的
            # 依赖项, 并执行生产启动门禁.
            # 该 fixture 仅限环回口: 开发环境/系统代理会拦截
            # 模型和对象存储调用 (socks 代理甚至会导致客户端构造失败),
            # 因此任何代理变量均不传递至子进程.
            child_env = {
                key: value for key, value in os.environ.items()
                if key.lower() not in {'http_proxy', 'https_proxy', 'all_proxy'}
            }
            child_env.update({
                'PYTHONUNBUFFERED': '1',
                'NO_PROXY': f'127.0.0.1,localhost,{bind_host}',
                'DATABASE_URL': postgres.url.render_as_string(hide_password=False),
                'SECRET_KEY': 'browser-fixture-signing-secret-' + uuid.uuid4().hex,
                # controller fixture 为其构建的容器配置了这两项;
                # API 必须共享它们才能连通真实运行时.
                'OPENCODE_SERVER_SECRET': os.environ['OPENCODE_SERVER_SECRET'],
                'RUNTIME_CONTROLLER_TOKEN': controller['token'],
                'RUNTIME_CONTROLLER_URL': controller['url'],
                'OPENCODE_IMAGE': runtime_image,
                'REDIS_HOST': '127.0.0.1',
                'REDIS_PORT': str(redis_port),
                'MINIO_ENDPOINT': minio_storage.integration_endpoint,
                'MINIO_ACCESS_KEY': minio_storage.integration_auth[0],
                'MINIO_SECRET_KEY': minio_storage.integration_auth[1],
                'MINIO_BUCKET': minio_storage.integration_bucket,
                'MINIO_SECURE': 'false',
                'UPLOAD_FOLDER': str(tmp_path / 'uploads'),
                'RABBITMQ_USER': 'browser-fixture-queue-user',
                'RABBITMQ_PASSWORD': 'browser-fixture-queue-password',
                'RABBITMQ_HOST': '127.0.0.1',
                'RABBITMQ_CONNECTION_ATTEMPTS': '1',
                'RABBITMQ_RECONNECT_DELAY_SECONDS': '0',
                'APP_ORIGIN': parent,
                'RUNTIME_UI_ORIGIN_TEMPLATE': f'http://r{{runtime_id}}.skycloud.localhost:{ui_port}',
                'CHAT_API_URL': f'http://127.0.0.1:{provider_port}/v1',
                'CHAT_API_KEY': PROVIDER_KEY,
                'CHAT_API_MODEL': CHAT_MODEL,
                'EMB_API_URL': f'http://127.0.0.1:{provider_port}/v1',
                'EMB_API_KEY': PROVIDER_KEY,
                'EMB_MODEL_NAME': EMB_MODEL,
            })
            api_command = [sys.executable, '-m', 'uvicorn', 'entry.api:app',
                           '--host', bind_host, '--port', str(api_port),
                           '--ws-max-size', '1048576', '--ws-max-queue', '4']
            with (tmp_path / 'api.log').open('w') as api_log:
                api_process = subprocess.Popen(api_command, cwd=backend, env=child_env,
                                               stdout=api_log, stderr=subprocess.STDOUT)
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                assert api_process.poll() is None, (tmp_path / 'api.log').read_text()[-4000:]
                try:
                    healthy = httpx.get(f'http://{bind_host}:{api_port}/api/health', timeout=2, trust_env=False)
                    if healthy.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(.2)
            else:
                raise AssertionError('Child API did not start: ' + (tmp_path / 'api.log').read_text()[-4000:])
            # 替换字面量地址: nginx 通过容器解析器解析基于变量的 proxy_pass
            # (绕过 /etc/hosts), 因此主机别名不可靠.
            template = (root / 'frontend/nginx.conf').read_text().replace(
                'backend-api:5000', f'{bind_host}:{api_port}')
            # 仅限测试专用的 include: 绝不修改生产 nginx.conf.
            template = template.replace(
                '    resolver 127.0.0.11 ipv6=off;\n',
                '    resolver 127.0.0.11 ipv6=off;\n    include /etc/nginx/fixture/usage-fault.conf;\n',
                1,
            )
            assert '/etc/nginx/fixture/usage-fault.conf' in template, (
                'Temporary nginx template is missing the fixture usage-fault include')
            config.write_text(template)
            gateway = ['--add-host', 'host.docker.internal:host-gateway'] if sys.platform.startswith('linux') else []
            docker('run', '-d', '--name', frontend, '--network', infrastructure['infra'], *gateway,
                   '-p', f'127.0.0.1:{parent_port}:80',
                   '-p', f'127.0.0.1:{ui_port}:8081', '-e', f'RUNTIME_UI_CSP_SOURCE=http://*.skycloud.localhost:{ui_port}',
                   '-v', str(config) + ':/etc/nginx/templates/default.conf.template:ro',
                   '-v', str(fault_include) + ':/etc/nginx/fixture/usage-fault.conf:ro',
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
                (FIXTURE_DIR / 'skycloud-full-browser-nginx.log').write_text(logs.stdout + logs.stderr)
            assert ready is not None and ready.status_code == 200, 'Isolated nginx cannot reach the loopback API'
            document = httpx.get(f'http://127.0.0.1:{parent_port}/', timeout=3, trust_env=False)
            assert "script-src 'self'" in document.headers['content-security-policy']
            assert 'unsafe-eval' not in document.headers['content-security-policy']
            def usage_log_status(path: str) -> int:
                return httpx.get(f'http://127.0.0.1:{parent_port}{path}', timeout=3,
                                 trust_env=False).status_code

            def wait_for_usage_logs(target: Callable[[int], bool]) -> tuple[int, int] | None:
                deadline = time.monotonic() + 10
                observed: tuple[int, int] | None = None
                while time.monotonic() < deadline:
                    observed = (usage_log_status('/api/token-usage/logs'),
                                usage_log_status('/api/admin/token-usage/logs'))
                    if all(target(status) for status in observed):
                        return observed
                    time.sleep(.2)
                return observed

            def match_502(status: int) -> bool:
                return status == 502

            def match_not_502(status: int) -> bool:
                return status != 502

            def apply_usage_fault(enabled: bool) -> None:
                fault_include.write_text(USAGE_FAULT_LOCATIONS if enabled else '')
                reloaded = docker('exec', frontend, 'nginx', '-s', 'reload', check=False)
                assert reloaded.returncode == 0, reloaded.stderr
                # Reload 会轮换 worker, 因此等待边界可被观测.
                target: Callable[[int], bool] = match_502 if enabled else match_not_502
                observed = wait_for_usage_logs(target)
                assert observed is not None
                assert all(target(status) for status in observed), (
                    'Fixture nginx usage-fault reload did not take effect', enabled, observed)
            fault['apply'] = apply_usage_fault
            # 在将边界移交浏览器前证明挂载的 include 已生效,
            # 随后使用空片段将其禁用.
            apply_usage_fault(True)
            apply_usage_fault(False)
            if os.getenv('SKYCLOUD_BROWSER_STARTUP_ONLY')=='1':
                return
            metadata.write_text(json.dumps({
                'url': parent,
                'username': username,
                'password': password,
                'workspace_id': workspace_ids[0],
                'workspace_ids': workspace_ids,
                'admin_username': admin_username,
                'admin_password': admin_password,
                'completion_file': str(completion),
                'model_release_url': f'http://127.0.0.1:{provider_port}/fixture/release',
                'usage_fault_url': f'http://127.0.0.1:{provider_port}/fixture/usage-fault',
                'usage_fault_include': str(fault_include),
                'nginx_container': frontend,
                'usage_samples': [
                    {'account': account, 'action': action, 'model_name': model,
                     'prompt_tokens': prompt, 'completion_tokens': completion_tokens, 'total_tokens': total}
                    for account, action, model, prompt, completion_tokens, total in USAGE_SAMPLES
                ],
            }))
            deadline = time.monotonic() + 1800
            while not completion.exists() and time.monotonic() < deadline:
                assert api_process.poll() is None, (
                    'Child API stopped during browser verification: '
                    + (tmp_path / 'api.log').read_text()[-4000:])
                time.sleep(.2)
            assert completion.exists(), 'Browser verification did not finish within thirty minutes'
            assert completion.read_text().strip() == 'verified', 'Browser verification failed'
        finally:
            metadata.unlink(missing_ok=True)
            closing.set(); released.set()
            if api_process is not None and api_process.poll() is None:
                api_process.terminate()
                try:
                    api_process.wait(10)
                except subprocess.TimeoutExpired:
                    api_process.kill(); api_process.wait(5)
