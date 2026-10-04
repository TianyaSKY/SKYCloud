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
from runtime_controller.policy import DEFAULT_IMAGE, POLICY_VERSION, Settings, container_name


@pytest.fixture
def settings():
    return Settings("T" * 48, "S" * 48, DEFAULT_IMAGE, "isolated-runtime-net", "1g", 100000, "skycloud")


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
    assert options["image"] == DEFAULT_IMAGE
    assert options["network"] == settings.network
    assert options["user"] == "10001:10001"
    assert options["read_only"] and options["cap_drop"] == ["ALL"]
    assert options["security_opt"] == ["no-new-privileges:true"]
    assert options["pids_limit"] == 128
    assert "volumes" not in options and "privileged" not in options
    assert "ports" not in options  # Runtime accessible only through the relay.
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
