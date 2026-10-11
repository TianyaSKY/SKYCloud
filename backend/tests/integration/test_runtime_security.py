"""Opt-in real Docker/Redis checks. All resources are uniquely named and removed.

RUN_RUNTIME_SECURITY_TESTS=1 python -m pytest tests/integration/test_runtime_security.py
"""

import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import time
import uuid
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from redis import Redis

from app.features.runtime_llm import quota

pytestmark = pytest.mark.skipif(os.getenv("RUN_RUNTIME_SECURITY_TESTS") != "1", reason="Opt-in isolated Docker integration tests")
IMAGE = os.getenv("SKYCLOUD_TEST_INFRA_IMAGE", "redis:7-alpine@sha256:858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499")
POSTGRES_IMAGE = "pgvector/pgvector:pg15@sha256:47af0b65960b14f1c6945aa4ccae37836c632e9a6fe0eea9160936e616e5b2e9"


def docker(*args, check=True):
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=30)
    if check and result.returncode:
        raise AssertionError(result.stderr)
    return result


@pytest.fixture
def infrastructure():
    prefix = "skycloud-security-test-" + uuid.uuid4().hex[:12]
    infra, runtime, control, target, bridge = [prefix + suffix for suffix in ("-infra", "-runtime", "-control", "-redis", "-bridge")]
    cleanup = [bridge, target]
    try:
        docker("network", "create", infra)
        docker("network", "create", control)
        docker("network", "create", "--internal", runtime)
        docker("run", "-d", "--name", target, "--network", infra, "-p", "127.0.0.1::6379", IMAGE)
        port = docker("port", target, "6379/tcp").stdout.strip().rsplit(":", 1)[1]
        client = Redis(host="127.0.0.1", port=int(port), decode_responses=True)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                if client.ping():
                    break
            except Exception:
                time.sleep(0.1)
        else:
            raise AssertionError("Isolated Redis did not start")
        yield {"infra": infra, "runtime": runtime, "control": control, "target": target, "bridge": bridge, "redis": client, "cleanup": cleanup}
        client.close()
    finally:
        for name in reversed(cleanup):
            docker("rm", "-f", "-v", name, check=False)
            if name.endswith('-controller'):
                docker('volume','rm',name+'-state',check=False)
        for name in (runtime, control, infra):
            docker("network", "rm", name, check=False)


def test_quota_is_atomic_across_concurrent_requests(infrastructure, monkeypatch):
    client = infrastructure["redis"]
    monkeypatch.setenv("RUNTIME_LLM_HOURLY_REQUEST_LIMIT", "7")
    credential = {"sub": "test-user", "workspace_id": 17, "runtime_id": 29, "quota": 100000}

    def invoke(_):
        try:
            quota.reserve(credential, 10, 20)
            return True
        except HTTPException as exc:
            assert exc.status_code == 429
            return False

    with patch.object(quota, "redis_client", client), concurrent.futures.ThreadPoolExecutor(max_workers=16) as workers:
        accepted = list(workers.map(invoke, range(40)))
    assert sum(accepted) == 7
    for key in client.scan_iter("runtime-llm-quota:*:requests"):
        assert int(client.get(key)) == 7
    for key in client.scan_iter("runtime-llm-quota:*:tokens"):
        assert int(client.get(key)) == 210


def test_quota_cannot_be_bypassed_by_recreating_runtime(infrastructure, monkeypatch):
    client = infrastructure["redis"]
    monkeypatch.setenv("RUNTIME_LLM_HOURLY_REQUEST_LIMIT", "1")
    credential = {"sub": "restart-user", "workspace_id": 18, "runtime_id": 30, "quota": 100000}
    with patch.object(quota, "redis_client", client):
        quota.reserve(credential, 10, 20)
        with pytest.raises(HTTPException) as denied:
            quota.reserve({**credential, "runtime_id": 31}, 10, 20)
    assert denied.value.status_code == 429
    assert not list(client.scan_iter("runtime-llm-quota:runtime:31:*"))


def test_internal_runtime_cannot_reach_infrastructure_by_name_or_ip(infrastructure):
    target = infrastructure["target"]
    inspect = json.loads(docker("inspect", target).stdout)[0]
    address = inspect["NetworkSettings"]["Networks"][infrastructure["infra"]]["IPAddress"]
    for host in (target, address):
        result = docker("run", "--rm", "--network", infrastructure["runtime"], "--entrypoint", "sh", IMAGE,
                        "-c", f"timeout 2 redis-cli -h {host} ping", check=False)
        assert result.returncode != 0
        assert "PONG" not in result.stdout
    # A dedicated gateway with both interfaces can reach infrastructure.
    docker("run", "-d", "--name", infrastructure["bridge"], "--network", infrastructure["infra"], IMAGE)
    docker("network", "connect", infrastructure["runtime"], infrastructure["bridge"])
    assert docker("exec", infrastructure["bridge"], "redis-cli", "-h", target, "ping").stdout.strip() == "PONG"


@pytest.mark.parametrize('development', [False, True], ids=['production', 'development'])
def test_production_ports_and_runtime_network_are_restricted(tmp_path, development):
    root = Path(__file__).resolve().parents[3]
    generated_env = tmp_path / "deployment.env"
    subprocess.run(["python3", str(root / "scripts/generate-env.py"), "--output", str(generated_env)], check=True, capture_output=True)
    compose = ["docker", "compose", "--env-file", str(generated_env), "-f", str(root / "docker-compose.yml")]
    if development:
        compose.extend(["-f", str(root / "docker-compose.dev.yml")])
    result = subprocess.run(
        [*compose, "config", "--format", "json"],
        env={**os.environ, "SKYCLOUD_ENV_FILE": str(generated_env),
             "OPENCODE_IMAGE": os.getenv("SKYCLOUD_TEST_RUNTIME_IMAGE", os.environ["OPENCODE_IMAGE"]),
             "MINIO_IMAGE": os.getenv("SKYCLOUD_TEST_MINIO_IMAGE", "sha256:" + "b" * 64)},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    config = json.loads(result.stdout)
    minio = config['services']['minio']
    assert minio['image'] == os.getenv('SKYCLOUD_TEST_MINIO_IMAGE', 'sha256:' + 'b' * 64)
    assert minio['pull_policy'] == 'never'
    assert minio['healthcheck']['test'][:2] == ['CMD', 'python']
    assert config["services"]["migrate"]["command"] == ["python", "-m", "entry.migrate"]
    assert not any(mount.get("target", "").startswith("/docker-entrypoint-initdb.d") for mount in config["services"]["db"].get("volumes", []))
    for service in ("backend-api", "backend-worker", "backend-mcp", "llm-gateway"):
        assert config["services"][service]["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
    for name, service in config["services"].items():
        if name != "frontend":
            ports = service.get("ports", [])
            if development:
                assert all(port.get('host_ip') == '127.0.0.1' for port in ports), name
            else:
                assert not ports, name
    if development:
        for name in ('db', 'redis', 'rabbitmq', 'minio', 'backend-api',
                     'backend-mcp', 'llm-gateway', 'runtime-controller'):
            assert config['services'][name].get('ports'), name
    assert config["networks"]["runtime-network"]["internal"] is True
    assert "runtime-network" not in config["services"]["backend-api"]["networks"]
    for name, service in config["services"].items():
        socket_mounts = [mount for mount in service.get("volumes", []) if mount.get("target") == "/var/run/docker.sock"]
        assert bool(socket_mounts) == (name == "runtime-controller")
    assert "env_file" not in config["services"]["runtime-controller"]
    for name in ("db", "redis", "rabbitmq", "minio"):
        assert "runtime-network" not in config["services"][name]["networks"]


def test_development_public_port_override_is_rejected(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[3]
    unsafe = tmp_path / 'unsafe-override.yml'
    unsafe.write_text((root / 'docker-compose.dev.yml').read_text().replace('127.0.0.1:', '0.0.0.0:', 1))
    actual_run = subprocess.run

    def render_override(args, **kwargs):
        # Only substitute this disposable Compose input; real Docker still
        # merges and validates the configuration, without starting containers.
        if args[:2] == ['docker', 'compose']:
            args = [str(unsafe) if arg == str(root / 'docker-compose.dev.yml') else arg for arg in args]
        return actual_run(args, **kwargs)

    monkeypatch.setattr(subprocess, 'run', render_override)
    with pytest.raises(AssertionError, match='db'):
        test_production_ports_and_runtime_network_are_restricted(tmp_path, True)


def test_postgresql_has_no_seeded_admin_and_bootstraps_once():
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session
    from entry.bootstrap_admin import bootstrap_admin

    root = Path(__file__).resolve().parents[3]
    name = "skycloud-bootstrap-test-" + uuid.uuid4().hex[:12]
    password = uuid.uuid4().hex + uuid.uuid4().hex
    engine = None
    try:
        docker("run", "-d", "--name", name, "-e", "POSTGRES_PASSWORD=" + password,
               "-p", "127.0.0.1::5432", "-v", str(root / "backend/init_db.sql") + ":/docker-entrypoint-initdb.d/init_db.sql:ro",
               POSTGRES_IMAGE)
        port = docker("port", name, "5432/tcp").stdout.strip().rsplit(":", 1)[1]
        engine = create_engine(f"postgresql://postgres:{password}@127.0.0.1:{port}/postgres")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                with engine.connect() as conn:
                    assert conn.scalar(text("SELECT count(*) FROM users")) == 0
                break
            except Exception:
                time.sleep(0.2)
        else:
            raise AssertionError("PostgreSQL initialization failed")
        from entry.migrate import upgrade
        upgrade(engine)

        def create(index):
            with Session(engine) as session:
                try:
                    return bootstrap_admin(session, f"operator-{index}", "unique-long-admin-password")
                except ValueError as exc:
                    assert "already exists" in str(exc)
                    return None

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(create, range(2)))
        winners = [result for result in results if result is not None]
        assert len(winners) == 1
        user_id = winners[0]
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT count(*) FROM users WHERE role='admin'")) == 1
            assert conn.scalar(text("SELECT owner_id FROM workspaces")) == user_id
            assert conn.scalar(text("SELECT count(*) FROM workspace_members WHERE role='admin'")) == 1
            assert conn.scalar(text("SELECT count(*) FROM folder WHERE parent_id IS NULL")) == 1
    finally:
        if engine:
            engine.dispose()
        docker("rm", "-f", "-v", name, check=False)


@pytest.fixture
def controller(infrastructure, monkeypatch):
    import httpx
    from runtime_controller.client import ControllerClient
    image = os.getenv("SKYCLOUD_TEST_RUNTIME_IMAGE", "")
    assert image, "Build scripts/build-runtime-image.py and set SKYCLOUD_TEST_RUNTIME_IMAGE to its immutable ID"
    from app.features.workspace import docker_service
    monkeypatch.setattr(docker_service, "OPENCODE_IMAGE", image)

    name = infrastructure["target"] + "-controller"
    infrastructure["cleanup"].append(name)
    token, secret = "T" * 48, "S" * 48
    gid = docker("run", "--rm", "--entrypoint", "stat", "-v", "/var/run/docker.sock:/var/run/docker.sock", IMAGE, "-c", "%g", "/var/run/docker.sock").stdout.strip()
    docker("run", "-d", "--name", name, "--network", infrastructure["control"], "--network-alias", "runtime-controller-control",
           "-p", "127.0.0.1::5003", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--group-add", gid,
           "-v", "/var/run/docker.sock:/var/run/docker.sock",
           "-v", name + "-state:/state",
           "-e", "RUNTIME_CONTROLLER_TOKEN=" + token, "-e", "OPENCODE_SERVER_SECRET=" + secret,
           "-e", "OPENCODE_IMAGE=" + image, "-e", "SKYCLOUD_DOCKER_NETWORK=" + infrastructure["runtime"],
           "-e", "RUNTIME_CONTROLLER_BIND_HOST=runtime-controller-control",
           "-e", "DOCKER_HOST=" + os.getenv('SKYCLOUD_TEST_DOCKER_PROXY_HOST','unix:///var/run/docker.sock'),
           os.getenv('SKYCLOUD_TEST_CONTROLLER_IMAGE', 'skycloud-runtime-controller:remediation-test'))
    docker("network", "connect", infrastructure["runtime"], name)
    port = docker("port", name, "5003/tcp").stdout.strip().rsplit(":", 1)[1]
    url = "http://127.0.0.1:" + port
    monkeypatch.setenv("RUNTIME_CONTROLLER_TOKEN", token)
    monkeypatch.setenv("RUNTIME_CONTROLLER_URL", url)
    monkeypatch.setenv("OPENCODE_SERVER_SECRET", secret)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            response = httpx.get(url + "/health", headers={"X-Runtime-Controller-Token": token}, timeout=1)
            if response.status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(.2)
    else:
        raise AssertionError(docker("logs", name).stderr)
    try:
        yield {"client": ControllerClient(), "url": url, "token": token, "name": name}
    finally:
        logs = docker("logs", name, check=False)
        Path("/private/tmp/skycloud-controller-integration.log").write_text(logs.stdout + logs.stderr)


def test_real_opencode_sandbox_boots_and_reloads_configuration(infrastructure, controller):
    import httpx
    from types import SimpleNamespace
    from app.features.workspace import docker_service as service
    from runtime_controller.client import auth_headers

    workspace = SimpleNamespace(id=int(uuid.uuid4().hex[:10], 16))
    name = f"skycloud-opencode-w{workspace.id}-u1"
    infrastructure["cleanup"].append(name)
    container = service._create(workspace, 1, 1)
    inspect = json.loads(docker("inspect", container.id).stdout)[0]
    snapshot = controller["client"].user_snapshot(1)
    assert {"id": container.id, "workspace_id": workspace.id, "user_id": 1, "runtime_id": 1, "status": "running"} in snapshot
    assert controller["client"].user_snapshot(2) == []
    policy = inspect["HostConfig"]
    assert policy["ReadonlyRootfs"] is True
    assert policy["CapDrop"] == ["ALL"]
    assert "no-new-privileges:true" in policy["SecurityOpt"]
    assert policy["PidsLimit"] == 128
    assert inspect["Config"]["User"] == "10001:10001"
    assert docker("exec", container.id, "id", "-u").stdout.strip() == "10001"
    assert docker("exec", container.id, "sh", "-c", "touch /etc/write-test", check=False).returncode != 0
    assert "size=512m" in policy["Tmpfs"]["/workspace"]
    assert not policy.get("PortBindings")
    assert not inspect["Mounts"]
    runtime = SimpleNamespace(container_id=container.id, id=1)
    base = service.get_runtime_base_url(workspace, 1, runtime)
    with httpx.Client(base_url=base, headers=auth_headers(base), timeout=10) as client:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                response = client.get("/global/health")
            except httpx.HTTPError:
                logs = docker("logs", container.id, check=False)
                Path("/private/tmp/skycloud-runtime-integration.log").write_text(logs.stdout + logs.stderr)
                raise
            if response.status_code == 200:
                break
            time.sleep(.2)
        else:
            raise AssertionError("Sandbox not reachable through Controller")
        ui = client.get(base.replace("/http", "/ui") + "/")
        assert ui.status_code == 200, (ui.status_code, ui.text[:200])
        assert "text/html" in ui.headers.get("content-type", ""), ui.headers
        Path("/private/tmp/skycloud-runtime-ui-root.html").write_text(ui.text)
        import re
        script = re.search(r'<script[^>]+src="([^"]+)', ui.text).group(1)
        asset = client.get(base.replace("/http", "/ui") + script)
        assert asset.status_code == 200
        assert "javascript" in asset.headers.get("content-type", "")
        Path("/private/tmp/skycloud-runtime-ui-main.js").write_text(asset.text)
        config = service.merge_opencode_chat_provider({}, {"base_url": "http://gateway:5002/v1", "model": "test-model", "api_key": "scoped-token"})
        service._write_opencode_config(container, config)
        assert service._read_opencode_config(container) == config
        assert client.post("/instance/dispose").status_code == 200
        loaded = client.get("/config")
        assert loaded.status_code == 200
        assert loaded.json()["model"] == "skycloud-chat/test-model"
        assert service._read_opencode_config(container)["provider"] == config["provider"]
    async def exercise_async_client():
        import asyncio
        from app.features.assistant.clients.opencode_client import OpenCodeClient
        async with OpenCodeClient(base, service.server_username(), service.server_password(1, 1, workspace.id), timeout=10) as client:
            assert (await client.health())["healthy"] is True
            session_id = await client.create_session("Controller relay session")
            assert (await client.get_session(session_id))["id"] == session_id
            stream = client.stream_events()
            try:
                event = await asyncio.wait_for(anext(stream), timeout=10)
                assert isinstance(event, dict) and event.get("type")
            finally:
                await stream.aclose()
    import asyncio
    asyncio.run(exercise_async_client())
    assert httpx.get(base + "/global/health", timeout=5).status_code == 401
    container.stop()
    assert controller["client"].get(container.id).status == "exited"
    assert next(row for row in controller["client"].user_snapshot(1) if row["id"] == container.id)["status"] == "stopped"
    container.start()
    assert controller["client"].get(container.id).status == "running"
    container.remove()
    assert not any(row["id"] == container.id for row in controller["client"].user_snapshot(1))


def test_runtime_network_cannot_contact_controller_rpc_listener(infrastructure, controller):
    inspect = json.loads(docker("inspect", controller["name"]).stdout)[0]
    networks = inspect["NetworkSettings"]["Networks"]
    image = inspect["Image"]
    # Supplying the valid service credential still cannot bypass binding the
    # listener to the separate control interface. Reuse the built image and
    # prove the same probe reaches the listener from its permitted network.
    script = """import http.client, sys
connection = http.client.HTTPConnection(sys.argv[1], 5003, timeout=2)
blocked = sys.argv[3] == 'blocked'
try:
    connection.connect()
except OSError:
    sys.exit(0 if blocked else 1)
else:
    if blocked:
        sys.exit(1)
    connection.request('GET', '/health', headers={'X-Runtime-Controller-Token': sys.argv[2]})
    response = connection.getresponse()
    sys.exit(0 if response.status == 200 else 1)
finally:
    connection.close()
"""
    for network, expected in ((infrastructure["control"], "allowed"),
                              (infrastructure["runtime"], "blocked")):
        address = networks[network]["IPAddress"]
        docker("run", "--rm", "--network", network, "--entrypoint", "python", image,
               "-c", script, address, controller["token"], expected)


def test_real_redis_ui_ticket_is_atomic_and_expires(infrastructure, monkeypatch):
    from app.features.workspace import ui_tickets
    client = infrastructure["redis"]
    monkeypatch.setattr(ui_tickets, "redis_client", client)
    raw = ui_tickets._store("ticket", {"runtime_id": 42}, 30)
    key = ui_tickets._key("ticket", raw)
    assert raw not in key and 0 < client.ttl(key) <= 30
    def redeem(_):
        try:
            return ui_tickets._load("ticket", raw, consume=True)
        except HTTPException as exc:
            assert exc.status_code == 401
            return None
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as workers:
        result = list(workers.map(redeem, range(32)))
    assert result.count({"runtime_id": 42}) == 1
    assert result.count(None) == 31
    raw = ui_tickets._store("session", {"runtime_id": 42}, 600)
    key = ui_tickets._key("session", raw)
    assert 0 < client.ttl(key) <= 600
    client.expire(key, 1)
    time.sleep(1.1)
    with pytest.raises(HTTPException) as exc:
        ui_tickets._load("session", raw)
    assert exc.value.status_code == 401
