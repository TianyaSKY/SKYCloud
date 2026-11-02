"""验证生产 Compose Worker 能删除 API 已删除的运行时."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

import httpx
import pytest

from tests.integration.fixture_paths import FIXTURE_DIR

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_WORKER_CONTROL_TESTS") != "1",
    reason="Opt-in real Compose Worker-to-Controller lifecycle",
)

SERVICES = (
    "db",
    "redis",
    "rabbitmq",
    "minio",
    "migrate",
    "runtime-controller",
    "backend-api",
    "backend-worker",
    "backend-mcp",
    "llm-gateway",
)
BACKEND_SERVICES = (
    "migrate",
    "backend-api",
    "backend-worker",
    "backend-mcp",
    "llm-gateway",
)
NETWORK_ALIASES = {
    "db": {"skycloud-network": ["skycloud-db"]},
    "redis": {"skycloud-network": ["skycloud-redis"]},
    "rabbitmq": {"skycloud-network": ["skycloud-rabbitmq"]},
    "minio": {"skycloud-network": ["skycloud-minio"]},
    "backend-api": {
        "skycloud-network": ["skycloud-backend-api"],
        "control-network": ["skycloud-backend-api"],
    },
    "backend-worker": {
        "skycloud-network": ["skycloud-backend-consumer"],
        "control-network": ["skycloud-backend-consumer"],
    },
    "backend-mcp": {
        "skycloud-network": ["skycloud-backend-mcp"],
        "runtime-network": ["skycloud-backend-mcp"],
    },
    "runtime-controller": {
        "control-network": ["runtime-controller-control", "skycloud-runtime-controller"],
        "runtime-network": ["skycloud-runtime-controller"],
    },
    "llm-gateway": {
        "skycloud-network": ["skycloud-llm-gateway"],
        "runtime-network": ["skycloud-llm-gateway"],
    },
}


def _run(
    command: list[str],
    *,
    env: dict[str, str],
    timeout: int,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and result.returncode:
        raise AssertionError(f"{command!r} failed: {result.stderr}")
    return result


def _generated_environment(path: Path, overrides: dict[str, str]) -> dict[str, str]:
    root = Path(__file__).resolve().parents[3]
    _run(
        [sys.executable, str(root / "scripts/generate-env.py"), "--output", str(path)],
        env=os.environ.copy(),
        timeout=30,
    )
    values = {
        key: value
        for line in path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
        for key, value in [line.split("=", 1)]
    }
    values.update(overrides)
    path.write_text(
        "\n".join(f"{key}={value}" for key, value in values.items()) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return values


def _compose_override(
    project: str,
    backend_image: str,
    controller_image: str,
) -> dict[str, object]:
    services: dict[str, object] = {}
    for service in SERVICES:
        config: dict[str, object] = {
            "container_name": f"{project}-{service}",
        }
        if service in BACKEND_SERVICES:
            config["image"] = backend_image
            config["build"] = None
        elif service == "runtime-controller":
            config["image"] = controller_image
            config["build"] = None
        if service == "backend-api":
            config["ports"] = ["127.0.0.1::5000"]
        if service in NETWORK_ALIASES:
            config["networks"] = {
                network: {"aliases": aliases}
                for network, aliases in NETWORK_ALIASES[service].items()
            }
        services[service] = config
    return {"services": services}


def _api_exec_json(
    run_compose,
    source: str,
    *,
    timeout: int = 30,
) -> object:
    result = run_compose(
        "exec",
        "-T",
        "backend-api",
        "python",
        "-c",
        source,
        timeout=timeout,
    )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        raise AssertionError("Controller RPC returned no JSON")
    return json.loads(lines[-1])


def _runtime_ids(
    *,
    user_id: int,
    workspace_id: int,
    runtime_id: int,
    short_id: str,
    project: str,
) -> list[str]:
    """仅查找匹配的完整 Docker 标识以进行精确的失败测试清理."""
    result = subprocess.run(
        [
            "docker",
            "ps",
            "-aq",
            "--filter",
            f"label=skycloud.user_id={user_id}",
            "--filter",
            f"label=skycloud.workspace_id={workspace_id}",
            "--filter",
            f"label=skycloud.runtime_id={runtime_id}",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        return []
    matches: list[str] = []
    for candidate in result.stdout.splitlines():
        inspected = subprocess.run(
            ["docker", "inspect", candidate],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if inspected.returncode:
            continue
        record = json.loads(inspected.stdout)[0]
        labels = record["Config"]["Labels"] or {}
        if (
            record["Id"].startswith(short_id)
            and labels.get("skycloud.user_id") == str(user_id)
            and labels.get("skycloud.workspace_id") == str(workspace_id)
            and labels.get("skycloud.runtime_id") == str(runtime_id)
            and labels.get("com.docker.compose.project") in {None, project}
        ):
            matches.append(record["Id"])
    return matches


def test_worker_removes_runtime_after_workspace_deletion(tmp_path):
    root = Path(__file__).resolve().parents[3]
    images = {
        "backend": os.getenv("SKYCLOUD_TEST_BACKEND_IMAGE", ""),
        "controller": os.getenv("SKYCLOUD_TEST_CONTROLLER_IMAGE", ""),
        "minio": os.getenv("SKYCLOUD_TEST_MINIO_IMAGE", ""),
        "runtime": os.getenv("SKYCLOUD_TEST_OPENCODE_IMAGE", ""),
    }
    missing = [name for name, image in images.items() if not image]
    assert not missing, "Set built test images: " + ", ".join(missing)
    assert images["minio"].startswith("sha256:") or "@sha256:" in images["minio"]
    assert images["runtime"].startswith("sha256:") or "@sha256:" in images["runtime"]

    project = "skycloud-worker-control-" + uuid.uuid4().hex[:12]
    env_file = tmp_path / "deployment.env"
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    overrides = {
        "UPLOAD_HOST_PATH": str(uploads),
        "MINIO_IMAGE": images["minio"],
        "MINIO_ACCESS_KEY": "worker-control-minio-test-access",
        "DOCKER_SOCKET_GID": str(Path("/var/run/docker.sock").stat().st_gid),
        "OPENCODE_IMAGE": images["runtime"],
        "CHAT_API_URL": "http://127.0.0.1:8080/v1",
        "CHAT_API_KEY": "worker-control-chat-test-key",
        "CHAT_API_MODEL": "worker-control-chat-test-model",
        "EMB_API_URL": "http://127.0.0.1:8080/v1",
        "EMB_API_KEY": "worker-control-embedding-test-key",
        "EMB_MODEL_NAME": "worker-control-embedding-test-model",
    }
    values = _generated_environment(env_file, overrides)
    override_file = tmp_path / "compose.override.yml"
    override_file.write_text(
        json.dumps(
            _compose_override(project, images["backend"], images["controller"]),
            indent=2,
        ),
        encoding="utf-8",
    )
    process_env = os.environ.copy()
    process_env.update(values)
    process_env.update(
        {
            "COMPOSE_PROJECT_NAME": project,
            "SKYCLOUD_ENV_FILE": str(env_file),
            "SKYCLOUD_TEST_BACKEND_IMAGE": images["backend"],
            "SKYCLOUD_TEST_CONTROLLER_IMAGE": images["controller"],
            "SKYCLOUD_TEST_MINIO_IMAGE": images["minio"],
            "SKYCLOUD_TEST_OPENCODE_IMAGE": images["runtime"],
        }
    )
    compose = [
        "docker",
        "compose",
        "--project-name",
        project,
        "--env-file",
        str(env_file),
        "-f",
        str(root / "docker-compose.yml"),
        "-f",
        str(override_file),
    ]

    def run_compose(*args: str, timeout: int = 30, check: bool = True):
        return _run(
            [*compose, *args],
            env=process_env,
            timeout=timeout,
            check=check,
        )

    user_id: int | None = None
    workspace_id: int | None = None
    runtime_id: int | None = None
    short_id: str | None = None
    full_container_id: str | None = None

    def save_logs() -> None:
        try:
            result = run_compose("logs", "--no-color", timeout=30, check=False)
            logs = result.stdout + result.stderr
        except Exception as error:
            logs = f"Compose logs unavailable ({type(error).__name__})\n"
        for name, secret in values.items():
            if secret and any(
                marker in name for marker in ("PASSWORD", "SECRET", "TOKEN", "_KEY")
            ):
                logs = logs.replace(secret, "<redacted>")
        try:
            FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
            (FIXTURE_DIR / f"{project}.log").write_text(logs, encoding="utf-8")
        except OSError:
            pass

    try:
        rendered = run_compose("config", "--format", "json")
        config = json.loads(rendered.stdout)
        worker = config["services"]["backend-worker"]
        assert worker["environment"]["RUNTIME_CONTROLLER_URL"] == (
            "http://runtime-controller-control:5003"
        )
        assert worker["depends_on"]["runtime-controller"]["condition"] == (
            "service_healthy"
        )
        assert "control-network" in worker["networks"]
        assert "runtime-network" not in worker["networks"]
        assert not worker.get("ports")
        assert not any(
            volume.get("target") == "/var/run/docker.sock"
            for volume in worker.get("volumes", [])
        )
        assert config["services"]["backend-api"]["ports"][0]["host_ip"] == (
            "127.0.0.1"
        )
        for service in SERVICES:
            assert config["services"][service]["container_name"] == (
                f"{project}-{service}"
            )

        run_compose("up", "-d", *SERVICES, timeout=180)
        published = run_compose("port", "backend-api", "5000").stdout.strip()
        api_port = published.rsplit(":", 1)[1]
        api_url = f"http://127.0.0.1:{api_port}"
        deadline = time.monotonic() + 180
        with httpx.Client(trust_env=False, timeout=5) as client:
            while time.monotonic() < deadline:
                try:
                    health = client.get(api_url + "/api/health")
                    if health.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.5)
            else:
                raise AssertionError("Compose API did not become healthy")

            username = "worker-control-" + uuid.uuid4().hex[:12]
            password = "worker-control-test-password-32"
            registered = client.post(
                api_url + "/api/auth/register",
                json={"username": username, "password": password},
            )
            assert registered.status_code == 201, registered.text
            login = client.post(
                api_url + "/api/auth/login",
                json={"username": username, "password": password},
            )
            assert login.status_code == 200, login.text
            token = login.json()["data"]["token"]
            headers = {"Authorization": f"Bearer {token}"}

            created = client.post(
                api_url + "/api/workspace",
                json={"name": "worker-control-delete"},
                headers=headers,
            )
            assert created.status_code == 201, created.text
            workspace_id = created.json()["data"]["id"]
            user_id = login.json()["data"]["user_id"]

            started = client.post(
                api_url + f"/api/workspace/{workspace_id}/opencode/runtime/start",
                headers=headers,
            )
            assert started.status_code == 200, started.text
            summary = started.json()["data"]
            assert summary["user_id"] == user_id
            runtime_id = summary["runtime_id"]
            short_id = summary["container_id"]
            assert runtime_id is not None
            assert isinstance(short_id, str) and len(short_id) == 12

            snapshot = _api_exec_json(
                run_compose,
                "import json; from runtime_controller.client import ControllerClient; "
                f"print(json.dumps(ControllerClient().user_snapshot({user_id})))",
            )
            matching = [
                row
                for row in snapshot
                if row["workspace_id"] == workspace_id
                and row["runtime_id"] == runtime_id
                and row["user_id"] == user_id
            ]
            assert len(matching) == 1
            full_container_id = matching[0]["id"]
            assert len(full_container_id) == 64
            assert full_container_id.startswith(short_id)

            # 正常停止仅停止容器; 仅工作区删除可将其移交给 Worker 进行物理删除.
            stopped = client.post(
                api_url + f"/api/workspace/{workspace_id}/opencode/runtime/stop",
                headers=headers,
            )
            assert stopped.status_code == 200, stopped.text
            assert stopped.json()["data"]["status"] == "stopped"
            kept = _api_exec_json(
                run_compose,
                "import json; from runtime_controller.client import ControllerClient; "
                f"print(json.dumps(ControllerClient().inspect({full_container_id!r})))",
            )
            assert kept["id"] == full_container_id
            assert kept["status"] != "running"

            deleted = client.delete(
                api_url + f"/api/workspace/{workspace_id}",
                headers=headers,
            )
            assert deleted.status_code == 200, deleted.text
            listed = client.get(api_url + "/api/workspace", headers=headers)
            assert listed.status_code == 200, listed.text
            workspaces = listed.json()["data"]["workspaces"]
            assert all(workspace["id"] != workspace_id for workspace in workspaces)

        poll = f"""import sys, time
from runtime_controller.client import ControllerClient, ControllerNotFound
client = ControllerClient()
deadline = time.monotonic() + 60
while time.monotonic() < deadline:
    try:
        payload = client.inspect({full_container_id!r})
    except ControllerNotFound:
        print('removed')
        raise SystemExit(0)
    if payload["id"] != {full_container_id!r}:
        raise SystemExit('Controller returned a different container identity')
    time.sleep(0.5)
print('Worker did not remove the Runtime within 60 seconds', file=sys.stderr)
raise SystemExit(1)
"""
        poll_result = run_compose(
            "exec", "-T", "backend-api", "python", "-c", poll, timeout=70,
        )
        assert poll_result.stdout.strip() == "removed"
    except BaseException:
        save_logs()
        raise
    finally:
        if full_container_id is None and all(
            value is not None for value in (user_id, workspace_id, runtime_id, short_id)
        ):
            candidates = _runtime_ids(
                user_id=user_id,
                workspace_id=workspace_id,
                runtime_id=runtime_id,
                short_id=short_id,
                project=project,
            )
            if len(candidates) == 1:
                full_container_id = candidates[0]
        if full_container_id is not None:
            subprocess.run(
                ["docker", "rm", "-f", full_container_id],
                capture_output=True,
                text=True,
                timeout=30,
            )
        run_compose("down", "--volumes", "--remove-orphans", timeout=180, check=False)
