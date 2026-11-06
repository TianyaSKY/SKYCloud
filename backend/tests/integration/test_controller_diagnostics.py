from typing import Any
# pyright: reportPrivateUsage=false
"""独立 Controller、实际 Uvicorn/Docker SDK 与合成守护进程故障测试."""
import os
import time
import httpx
import pytest
from tests.integration.test_runtime_security import infrastructure, controller, docker

__all__ = ["infrastructure", "controller", "docker"]

pytestmark = pytest.mark.skipif(os.getenv('RUN_CONTROLLER_DIAGNOSTICS') != '1', reason='Opt-in rebuilt Controller diagnostics')
SECRET = 'private-controller-daemon-body-sentinel'


@pytest.fixture
def fault_controller(request: Any, infrastructure: Any, monkeypatch: Any):
    # 这是一个守护进程协议故障接缝, 并非真实 Docker 管理端点.
    name = infrastructure['target'] + '-fault-daemon'
    infrastructure['cleanup'].append(name)
    script = '''
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        version = self.path == '/version'
        body = json.dumps({'ApiVersion':'1.45', 'Version':'26.0.0'} if version else
                          {'message':'private-controller-daemon-body-sentinel'}).encode()
        self.send_response(200 if version else 500)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(body)))
        self.end_headers();self.wfile.write(body)
    def log_message(self,*args):pass
HTTPServer(('0.0.0.0',2375),Handler).serve_forever()
'''
    docker('run','-d','--name',name,'--network',infrastructure['control'],'--network-alias','diagnostic-daemon',
           '--read-only','--cap-drop','ALL','--security-opt','no-new-privileges:true',
           '--entrypoint','python',os.environ['SKYCLOUD_TEST_CONTROLLER_IMAGE'],'-u','-c',script)
    monkeypatch.setenv('SKYCLOUD_TEST_DOCKER_PROXY_HOST','tcp://diagnostic-daemon:2375')
    return request.getfixturevalue('controller')


def test_daemon_private_failure_is_absent_from_http_and_standalone_logs(fault_controller: Any) -> None:
    headers = {'X-Runtime-Controller-Token':fault_controller['token']}
    base = fault_controller['url']
    with httpx.Client(trust_env=False, timeout=10) as client:
        response = client.post(
            base + '/containers/prepare',
            headers=headers,
            json={'workspace_id':1,'user_id':1,'runtime_id':1,'generation':'a'*32},
        )
        assert response.status_code == 500
        assert response.json() == {'detail':'Runtime controller operation failed'}
        denied = client.get(base + '/containers/' + SECRET, headers=headers)
        assert denied.status_code == 403
        assert denied.json() == {'detail':'Runtime operation forbidden'}
        assert client.get(base + '/health', headers=headers).status_code == 200
    deadline = time.monotonic() + 5
    while True:
        logs = docker('logs',fault_controller['name'])
        output = logs.stdout + logs.stderr
        if 'Library failure in uvicorn.error (error_type=APIError)' in output:
            break
        assert time.monotonic() < deadline, output
        time.sleep(.05)
    assert 'Runtime controller operation failed (error_type=APIError)' in output
    assert SECRET not in output and 'Traceback' not in output
