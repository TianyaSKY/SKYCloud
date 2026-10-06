"""Controller authentication, fixed Docker policy, identity and relay regressions."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from runtime_controller.app import MAX_BODY, create_app
from runtime_controller.client import ControllerClient, ControllerError, auth_headers, runtime_url
from runtime_controller.engine import Engine, MissingRuntime, PolicyViolation
from runtime_controller.policy import POLICY_VERSION, Settings, container_name

TEST_IMAGE = 'test/runtime@sha256:' + 'a' * 64


@pytest.fixture
def settings():
    return Settings("T" * 48, "S" * 48, TEST_IMAGE, "isolated-runtime-net", "1g", 100000, "skycloud")


@pytest.fixture
def managed_container(settings):
    options = settings.container_options(2, 3, 4)
    container = Mock()
    container.id = "a" * 64
    container.name = container_name(2, 3)
    container.status = "running"
    container.attrs = {
        "Config": {"Labels": options["labels"], "Image": settings.image, "User": options["user"], "Env": ["SECRET=not-for-the-api"]},
        "HostConfig": {"ReadonlyRootfs": True, "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges:true"], "Binds": None, "Privileged": False},
        "Mounts": [], "NetworkSettings": {"Ports": {}, "Networks": {settings.network: {"IPAddress": "172.20.0.2"}}},
    }
    return container


def test_fixed_policy_excludes_host_mounts_and_secrets(settings):
    options = settings.container_options(2, 3, 4)
    assert options["image"] == TEST_IMAGE
    assert options["network"] == settings.network
    assert options["user"] == "10001:10001"
    assert options["read_only"] and options["cap_drop"] == ["ALL"]
    assert options["security_opt"] == ["no-new-privileges:true"]
    assert options["pids_limit"] == 128
    assert "volumes" not in options and "privileged" not in options
    assert "ports" not in options  # Runtime accessible only through the relay.
    assert "noexec" in options["tmpfs"]["/tmp"]
    assert options["environment"]["TMPDIR"] == "/tmp/.native"
    assert "exec" in options["tmpfs"]["/tmp/.native"]
    assert "size=64m" in options["tmpfs"]["/tmp/.native"]
    assert settings.token not in str(options)
    assert settings.server_secret not in str(options)
    assert "CHAT_API_KEY" not in str(options)


def test_controller_cannot_inspect_other_containers(settings, managed_container):
    client = Mock()
    client.containers.get.return_value = managed_container
    engine = Engine(settings, client)
    for ref in ("db", "/var/run/docker.sock", "../redis", "http://host", "0"):
        with pytest.raises(PolicyViolation):
            engine.inspect(ref)
    client.containers.get.assert_not_called()
    managed_container.attrs["Config"]["Labels"]["skycloud.component"] = "database"
    with pytest.raises(PolicyViolation):
        engine.inspect("a" * 64)


@pytest.mark.parametrize("tamper", ["name", "owner", "image", "root", "mount", "network", "policy"])
def test_managed_labels_do_not_override_container_policy(settings, managed_container, tamper):
    if tamper == "name":
        managed_container.name = "redis"
    elif tamper == "owner":
        managed_container.attrs["Config"]["Labels"]["skycloud.user_id"] = "7"
    elif tamper == "image":
        managed_container.attrs["Config"]["Image"] = "evil:latest"
    elif tamper == "root":
        managed_container.attrs["Config"]["User"] = "root"
    elif tamper == "mount":
        managed_container.attrs["Mounts"] = [{"Source": "/", "Destination": "/host"}]
    elif tamper == "network":
        managed_container.attrs["NetworkSettings"]["Networks"]["infra"] = {"IPAddress": "172.21.0.2"}
    elif tamper == "policy":
        managed_container.attrs["Config"]["Labels"]["skycloud.runtime_policy"] = "old"
    client = Mock()
    client.containers.get.return_value = managed_container
    with pytest.raises(PolicyViolation):
        Engine(settings, client).action("a" * 64, "start")
    managed_container.start.assert_not_called()


def test_inspection_never_returns_environment(settings, managed_container):
    managed_container.attrs["Config"]["Labels"]["secret-label"] = "private"
    client = Mock()
    client.containers.get.return_value = managed_container
    result = Engine(settings, client).inspect("a" * 64)
    assert "not-for-the-api" not in str(result)
    assert "secret-label" not in str(result)
    assert result["labels"]["skycloud.runtime_policy"] == POLICY_VERSION


def test_request_auth_and_no_arbitrary_docker_api(settings):
    engine = Mock()
    with TestClient(create_app(settings, engine), raise_server_exceptions=False) as client:
        assert client.post("/containers/create", json={}).status_code == 401
        headers = {"X-Runtime-Controller-Token": settings.token}
        response = client.post("/containers/create", headers=headers, json={"workspace_id": 2, "user_id": 3, "runtime_id": 4, "volumes": {"/": "/host"}})
        assert response.status_code == 422
        engine.create.assert_not_called()
        assert client.post("/containers/" + "a" * 64 + "/actions/exec", headers=headers).status_code == 422
        assert client.post("/containers/" + "a" * 64 + "/exec", headers=headers, json={"cmd": "cat /etc/shadow"}).status_code == 404
        response = client.put("/containers/" + "a" * 64 + "/config", headers=headers, content=b"a" * (MAX_BODY + 1))
        assert response.status_code == 413
        engine.write_config.assert_not_called()


def test_config_writer_never_executes_caller_commands(settings, managed_container):
    from runtime_controller.policy import RUNTIME_USER

    client = Mock()
    client.containers.get.return_value = managed_container
    managed_container.exec_run.return_value = SimpleNamespace(exit_code=0)
    payload = {"prompt": "$(cat /etc/shadow); `touch /host`"}
    Engine(settings, client).write_config("a" * 64, payload)
    args, kwargs = managed_container.exec_run.call_args
    assert "$(cat" not in args[0][2] and "touch /host" not in args[0][2]
    assert kwargs["user"] == RUNTIME_USER
    assert "umask 077" in args[0][2]


def test_http_relay_strips_control_and_caller_credentials(settings):
    engine = Mock()
    engine.target.return_value = ("http://172.20.0.2:3000", ("skycloud", "runtime-password"))
    requests = []
    real_client = httpx.AsyncClient

    def transport(request):
        requests.append(request)
        return httpx.Response(200, json={"healthy": True})

    with TestClient(create_app(settings, engine), raise_server_exceptions=False) as client, patch(
        "runtime_controller.app.httpx.AsyncClient", side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(transport), **kwargs)
    ):
        headers = {"X-Runtime-Controller-Token": settings.token, "Authorization": "Bearer not-a-runtime-password"}
        response = client.get("/runtimes/" + "a" * 64 + "/http/global/health", headers=headers)
        assert response.status_code == 200
        assert response.json() == {"healthy": True}
        assert "x-runtime-controller-token" not in requests[0].headers
        assert requests[0].headers["authorization"] != headers["Authorization"]
        assert requests[0].url.host == "172.20.0.2"
        assert client.get("/runtimes/" + "a" * 64 + "/http/http://db", headers=headers).status_code == 403


def test_http_relay_streams_and_closes_upstream(settings):
    engine = Mock()
    engine.target.return_value = ("http://172.20.0.2:3000", ("skycloud", "runtime-password"))
    real_client = httpx.AsyncClient
    response = httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=b"data: {\"type\":\"connected\"}\n\n")
    with TestClient(create_app(settings, engine), raise_server_exceptions=False) as client, patch(
        "runtime_controller.app.httpx.AsyncClient", side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(lambda _: response), **kwargs)
    ):
        result = client.get("/runtimes/" + "a" * 64 + "/http/event", headers={"X-Runtime-Controller-Token": settings.token})
    assert "data:" in result.text
    assert response.is_closed


def test_control_token_is_sent_only_to_controller_url(monkeypatch):
    monkeypatch.setenv("RUNTIME_CONTROLLER_TOKEN", "private-controller-credential")
    monkeypatch.setenv("RUNTIME_CONTROLLER_URL", "http://controller:5003")
    assert auth_headers("http://external.example:3000") == {}
    assert auth_headers("http://controller:5003.evil/runtimes/x/http") == {}
    assert auth_headers(runtime_url("a" * 64))["X-Runtime-Controller-Token"] == "private-controller-credential"


def test_user_snapshot_is_one_filtered_docker_request(settings):
    from copy import deepcopy
    labels = settings.container_options(2, 3, 4)['labels']
    good = dict(Id='a' * 64, Names=['/skycloud-opencode-w2-u3'], State='running',
                Labels=labels, Env=['PRIVATE'], Ports=[{'PublicPort': 1}])
    foreign = deepcopy(good); foreign['Labels']['skycloud.user_id'] = '9'
    invalid = deepcopy(good); invalid['Names'] = ['/database']
    client = Mock(); client.api.containers.return_value = [good, foreign, invalid]
    result = Engine(settings, client).user_snapshot(3)
    assert result == [dict(id='a' * 64, workspace_id=2, user_id=3, runtime_id=4, status='running')]
    client.api.containers.assert_called_once_with(all=True, filters={'label': [
        'skycloud.component=opencode-runtime', 'skycloud.user_id=3']})
    client.containers.get.assert_not_called()
    assert 'PRIVATE' not in str(result) and 'PublicPort' not in str(result)


def test_user_snapshot_endpoint_requires_auth_and_positive_identity(settings):
    engine = Mock(); engine.user_snapshot.return_value = []
    with TestClient(create_app(settings, engine)) as client:
        assert client.get('/users/3/containers').status_code == 401
        headers = {'X-Runtime-Controller-Token': settings.token}
        assert client.get('/users/0/containers', headers=headers).status_code == 422
        assert client.get('/users/3/containers', headers=headers).json() == []
    engine.user_snapshot.assert_called_once_with(3)


def test_controller_socket_auth_and_fixed_target(settings):
    from tests.test_runtime_ui_socket import EchoSocket
    engine = Mock(); engine.target.return_value = ('http://172.20.0.2:3000', ('runtime-user', 'server-secret'))
    requests = []; peers = []
    def connect(url, headers):
        requests.append((url, headers)); peer = EchoSocket(); peers.append(peer); return peer
    path = '/runtimes/' + 'a' * 64 + '/ws/pty/pty_42/connect'
    with TestClient(create_app(settings, engine)) as client, patch('runtime_controller.app.direct_connect', side_effect=connect):
        with pytest.raises(Exception) as exc:
            with client.websocket_connect(path):
                pass
        assert exc.value.code == 1008
        engine.target.assert_not_called()
        headers = {'X-Runtime-Controller-Token': settings.token, 'Authorization': 'Bearer caller', 'Cookie': 'parent=session'}
        with client.websocket_connect(path + '?cursor=0', headers=headers) as socket:
            socket.send_text('terminal text'); assert socket.receive_text() == 'terminal text'
        assert requests[0][0] == 'ws://172.20.0.2:3000/pty/pty_42/connect?cursor=0'
        assert set(requests[0][1]) == {'Authorization'}
        import base64
        assert requests[0][1]['Authorization'] == 'Basic ' + base64.b64encode(b'runtime-user:server-secret').decode()
        with pytest.raises(Exception) as exc:
            with client.websocket_connect(path + '?auth_token=caller', headers=headers):
                pass
        assert exc.value.code == 1008


async def test_websocket_redirect_never_reaches_another_host():
    from runtime_controller.socket_relay import direct_connect
    from websockets.exceptions import InvalidStatus
    hits = []
    async def attacker(reader, writer):
        hits.append(await reader.readuntil(b'\r\n\r\n'))
        writer.close(); await writer.wait_closed()
    async with await asyncio.start_server(attacker, '127.0.0.1', 0) as target:
        port = target.sockets[0].getsockname()[1]
        async def redirect(reader, writer):
            await reader.readuntil(b'\r\n\r\n')
            response = f'HTTP/1.1 307 Temporary Redirect\r\nLocation: ws://127.0.0.1:{port}/steal\r\nContent-Length: 0\r\nConnection: close\r\n\r\n'
            writer.write(response.encode()); await writer.drain()
            writer.close(); await writer.wait_closed()
        async with await asyncio.start_server(redirect, '127.0.0.1', 0) as server:
            origin_port = server.sockets[0].getsockname()[1]
            with pytest.raises(InvalidStatus):
                async with direct_connect(f'ws://127.0.0.1:{origin_port}/terminal', {'Authorization': 'Bearer test-only-secret'}):
                    raise AssertionError('Redirect was followed')
    assert hits == []


@pytest.mark.parametrize('name',['skycloud-workspace-2','skycloud-opencode-w2-u3'])
def test_legacy_cleanup_exact_identity_is_authenticated_and_removal_only(settings,managed_container,name):
    managed_container.name = name
    managed_container.attrs['Config']['Labels']['skycloud.runtime_id'] = ''
    managed_container.attrs['Config']['Labels'].pop('skycloud.runtime_policy')
    client = Mock()
    client.containers.get.return_value = managed_container
    engine = Engine(settings,client)
    http = TestClient(create_app(settings,engine))
    path = '/retired-containers/'+managed_container.id+'/remove'
    assert http.post(path).status_code == 401
    managed_container.remove.assert_not_called()
    assert http.post(path,headers={'X-Runtime-Controller-Token':settings.token}).json() == {'success':True}
    managed_container.remove.assert_called_once_with(force=True,v=True)
    for action in ('start','stop','restart'):
        with pytest.raises(PolicyViolation): engine.action(managed_container.id,action)
    with pytest.raises(PolicyViolation): engine.target(managed_container.id)
    with pytest.raises(PolicyViolation): engine.read_config(managed_container.id)


@pytest.mark.parametrize('tamper',['short','alias','id','name','component','workspace','user','missing_user','missing_runtime','modern_runtime'])
def test_legacy_cleanup_rejects_ambiguous_or_unmanaged_identity(settings,managed_container,tamper):
    managed_container.name = 'skycloud-workspace-2'
    labels = managed_container.attrs['Config']['Labels']
    labels['skycloud.runtime_id'] = ''
    ref = managed_container.id
    if tamper == 'short': ref = ref[:12]
    elif tamper == 'alias': ref = managed_container.name
    elif tamper == 'id': managed_container.id = 'b'*64
    elif tamper == 'name': managed_container.name = 'redis'
    elif tamper == 'component': labels['skycloud.component'] = 'database'
    elif tamper == 'workspace': labels['skycloud.workspace_id'] = '0'
    elif tamper == 'user': labels['skycloud.user_id'] = '0'
    elif tamper == 'missing_user': del labels['skycloud.user_id']
    elif tamper == 'missing_runtime': del labels['skycloud.runtime_id']
    elif tamper == 'modern_runtime': labels['skycloud.runtime_id'] = '4'
    client = Mock()
    client.containers.get.return_value = managed_container
    with pytest.raises(PolicyViolation): Engine(settings,client).remove_legacy(ref)
    managed_container.remove.assert_not_called()


def test_unsupported_legacy_cleanup_route_404_is_not_confirmed_absence(monkeypatch):
    from runtime_controller.client import ControllerNotFound
    monkeypatch.setenv('RUNTIME_CONTROLLER_TOKEN','T'*48)
    monkeypatch.setattr(httpx,'request',lambda *args,**kwargs:httpx.Response(404,json={'detail':'Not Found'}))
    with pytest.raises(ControllerError) as failure:
        ControllerClient().remove_legacy('a'*64)
    assert not isinstance(failure.value,ControllerNotFound)


def test_prepare_schema_requires_generation_and_forbids_docker_options(settings):
    engine = Mock()
    engine.prepare.return_value = {'id':'a'*64}
    http = TestClient(create_app(settings,engine))
    headers = {'X-Runtime-Controller-Token':settings.token}
    body = {'workspace_id':2,'user_id':3,'runtime_id':4,'generation':'a'*32}
    assert http.post('/containers/prepare',json=body).status_code == 401
    for extra in ({'generation':'../bad'},{'generation':'a'*33},{'volumes':['/']},{'runtime_id':0}):
        assert http.post('/containers/prepare',json={**body,**extra},headers=headers).status_code == 422
    engine.prepare.assert_not_called()
    assert http.post('/containers/prepare',json=body,headers=headers).status_code == 200
    engine.prepare.assert_called_once_with(**body)


@pytest.mark.parametrize('tamper',['missing_generation','wrong_generation','wrong_name','bad_generation'])
def test_generated_name_must_match_generation_label(settings,managed_container,tamper):
    from runtime_controller.policy import generation_name
    managed_container.name = generation_name(2,3,'a'*32)
    labels = managed_container.attrs['Config']['Labels']
    labels['skycloud.generation'] = 'a'*32
    if tamper == 'missing_generation': del labels['skycloud.generation']
    elif tamper == 'wrong_generation': labels['skycloud.generation'] = 'b'*32
    elif tamper == 'wrong_name': managed_container.name = container_name(2,3)
    elif tamper == 'bad_generation': labels['skycloud.generation'] = '../bad'
    client = Mock()
    client.containers.get.return_value = managed_container
    with pytest.raises(PolicyViolation): Engine(settings,client).inspect('a'*64)


def test_prepare_replay_requires_same_runtime_and_current_sandbox(settings,managed_container):
    from runtime_controller.policy import generation_name
    managed_container.name = generation_name(2,3,'a'*32)
    managed_container.attrs['Config']['Labels']['skycloud.generation'] = 'a'*32
    client = Mock()
    client.networks.get.return_value.attrs = {'Internal':True}
    client.containers.get.return_value = managed_container
    engine = Engine(settings,client)
    assert engine.prepare(2,3,4,'a'*32)['id'] == managed_container.id
    with pytest.raises(PolicyViolation): engine.prepare(2,3,5,'a'*32)
    managed_container.attrs['HostConfig']['Privileged'] = True
    with pytest.raises(PolicyViolation): engine.prepare(2,3,4,'a'*32)
    managed_container.remove.assert_not_called()
    managed_container.start.assert_not_called()
    client.containers.create.assert_not_called()


def test_prepare_rejects_implicit_image_volume_and_cleans_only_new_container(settings,managed_container):
    from runtime_controller.policy import generation_name
    managed_container.name = generation_name(2,3,'a'*32)
    managed_container.attrs['Config']['Labels']['skycloud.generation'] = 'a'*32
    managed_container.attrs['Mounts'] = [{'Type':'volume','Destination':'/data'}]
    client = Mock()
    client.networks.get.return_value.attrs = {'Internal':True}
    client.containers.get.side_effect = [MissingRuntime('absent'),managed_container]
    client.containers.create.return_value = managed_container
    with pytest.raises(PolicyViolation): Engine(settings,client).prepare(2,3,4,'a'*32)
    managed_container.remove.assert_called_once_with(force=True,v=True)
    managed_container.start.assert_not_called()
    client.containers.run.assert_not_called()


def test_prepare_unknown_endpoint_404_never_requests_destructive_create(monkeypatch):
    from runtime_controller.client import ControllerNotFound
    monkeypatch.setenv('RUNTIME_CONTROLLER_TOKEN','T'*48)
    requests = []
    def unavailable(method,url,**kwargs):
        requests.append(url)
        return httpx.Response(404,json={'detail':'Not Found'})
    monkeypatch.setattr(httpx,'request',unavailable)
    with pytest.raises(ControllerError) as failure: ControllerClient().prepare(2,3,4,'a'*32)
    assert not isinstance(failure.value,ControllerNotFound)
    assert len(requests) == 1 and requests[0].endswith('/containers/prepare')
