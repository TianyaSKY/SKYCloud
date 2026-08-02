"""Opt-in end-to-end checks for the official OpenCode expert runtime.

These tests deliberately use the Docker CLI instead of the mocked ``docker``
module installed by the unit-test conftest.  They are skipped by default and
require ``RUN_OPENCODE_E2E=1``.  The model test additionally requires
``RUN_OPENCODE_MODEL_E2E=1`` and an OpenAI-compatible endpoint reachable from
inside Docker.
"""

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.features.assistant import repository, service
from app.features.assistant.clients.opencode_auth import server_password, server_username
from app.features.assistant.clients.opencode_client import OpenCodeClient
from app.features.workspace import docker_service, runtime_service


E2E_ENABLED = os.getenv("RUN_OPENCODE_E2E", "0") == "1"
MODEL_E2E_ENABLED = os.getenv("RUN_OPENCODE_MODEL_E2E", "0") == "1"
DOCKER_AVAILABLE = shutil.which("docker") is not None

pytestmark = pytest.mark.skipif(
    not E2E_ENABLED or not DOCKER_AVAILABLE,
    reason="设置 RUN_OPENCODE_E2E=1 且安装 Docker CLI 后启用",
)

IMAGE = os.getenv("OPENCODE_E2E_IMAGE", "ghcr.io/anomalyco/opencode:latest")
USERNAME = os.getenv("OPENCODE_SERVER_USERNAME", "skycloud")
STARTUP_TIMEOUT = max(float(os.getenv("OPENCODE_E2E_STARTUP_TIMEOUT", "60")), 10.0)
MODEL_TIMEOUT = max(float(os.getenv("OPENCODE_E2E_MODEL_TIMEOUT", "180")), 30.0)


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["docker", *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise AssertionError(f"Docker 命令失败: docker {' '.join(args)}\n{detail}")
    return result


def _chat_config() -> dict[str, str]:
    values = {
        "base_url": os.getenv("OPENCODE_E2E_CHAT_API_URL", "").strip().rstrip("/"),
        "api_key": os.getenv("OPENCODE_E2E_CHAT_API_KEY", "").strip(),
        "model": os.getenv("OPENCODE_E2E_CHAT_API_MODEL", "").strip(),
    }
    missing = [name for name, value in values.items() if not value]
    if missing:
        pytest.skip(
            "专家模型 E2E 需要设置 "
            + ", ".join(
                {
                    "base_url": "OPENCODE_E2E_CHAT_API_URL",
                    "api_key": "OPENCODE_E2E_CHAT_API_KEY",
                    "model": "OPENCODE_E2E_CHAT_API_MODEL",
                }[name]
                for name in missing
            )
        )
    return values


def _build_config(chat: dict[str, str] | None) -> dict[str, Any]:
    config: dict[str, Any] = {}
    if chat:
        config = docker_service.merge_opencode_chat_provider(config, chat)
    known_agent = docker_service.merge_opencode_expert_agent({})["agent"][
        docker_service.OPENCODE_EXPERT_AGENT
    ]
    config["agent"] = {"skycloud-e2e": known_agent}
    return config


class _OpenCodeRuntime:
    def __init__(
        self,
        *,
        chat: dict[str, str] | None,
        runtime_id: int = 900001,
        user_id: int = 900001,
        workspace_id: int = 900001,
    ):
        self.chat = chat
        self.runtime_id = runtime_id
        self.user_id = user_id
        self.workspace_id = workspace_id
        self.name = f"skycloud-opencode-e2e-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.password = server_password(runtime_id, user_id, workspace_id)
        self._temp_dir: tempfile.TemporaryDirectory[str] | None = None
        self.base_url: str | None = None

    def __enter__(self) -> "_OpenCodeRuntime":
        self._temp_dir = tempfile.TemporaryDirectory(prefix="skycloud-opencode-e2e-")
        try:
            config_dir = Path(self._temp_dir.name)
            (config_dir / "opencode.json").write_text(
                json.dumps(_build_config(self.chat), ensure_ascii=False),
                encoding="utf-8",
            )
            image_check = _docker("image", "inspect", IMAGE, check=False)
            if image_check.returncode != 0:
                _docker("pull", IMAGE)

            _docker(
                "run",
                "--detach",
                "--rm",
                "--name",
                self.name,
                "--env",
                f"OPENCODE_SERVER_USERNAME={USERNAME}",
                "--env",
                f"OPENCODE_SERVER_PASSWORD={self.password}",
                "--publish",
                "127.0.0.1::3000",
                IMAGE,
                "serve",
                "--hostname",
                "0.0.0.0",
                "--port",
                "3000",
            )
            port = self._wait_for_port()
            self.base_url = f"http://127.0.0.1:{port}"
            self._wait_for_health()
            _docker("exec", self.name, "mkdir", "-p", "/root/.config/opencode")
            _docker(
                "cp",
                str(config_dir / "opencode.json"),
                f"{self.name}:/root/.config/opencode/opencode.json",
            )
            _docker("restart", self.name)
            port = self._wait_for_port()
            self.base_url = f"http://127.0.0.1:{port}"
            self._wait_for_health()
            return self
        except BaseException:
            self._cleanup(show_logs=True)
            raise

    def __exit__(self, exc_type, exc, tb) -> None:
        self._cleanup(show_logs=exc_type is not None)

    def _cleanup(self, *, show_logs: bool) -> None:
        if show_logs:
            logs = _docker("logs", self.name, check=False)
            if logs.stdout.strip() or logs.stderr.strip():
                print("\nOpenCode E2E 容器日志:\n" + (logs.stdout + logs.stderr)[-8000:])
        _docker("rm", "--force", self.name, check=False)
        if self._temp_dir is not None:
            self._temp_dir.cleanup()

    def _wait_for_port(self) -> int:
        deadline = time.monotonic() + STARTUP_TIMEOUT
        while time.monotonic() < deadline:
            result = _docker("port", self.name, "3000/tcp", check=False)
            match = re.search(r":(\d+)\s*$", result.stdout.strip())
            if match:
                return int(match.group(1))
            time.sleep(0.5)
        logs = _docker("logs", self.name, check=False)
        raise AssertionError(f"OpenCode 容器未发布端口：\n{logs.stdout[-4000:]}")

    def _wait_for_health(self) -> None:
        assert self.base_url is not None

        async def poll() -> None:
            deadline = time.monotonic() + STARTUP_TIMEOUT
            last_error: Exception | None = None
            while time.monotonic() < deadline:
                try:
                    async with OpenCodeClient(
                        self.base_url,
                        USERNAME,
                        self.password,
                        timeout=3,
                    ) as client:
                        health = await client.health()
                        if health.get("healthy", True):
                            return
                except Exception as exc:  # startup is intentionally retried
                    last_error = exc
                await asyncio.sleep(0.5)
            raise AssertionError(f"OpenCode 健康检查超时: {last_error}")

        asyncio.run(poll())


def test_official_opencode_runtime_health_mcp_and_session() -> None:
    """The official image accepts auth, MCP config, and session operations."""

    with _OpenCodeRuntime(chat=_chat_config()) as runtime:
        assert runtime.base_url is not None
        mcp_url = os.getenv("OPENCODE_E2E_MCP_URL", "").strip()

        async def exercise() -> tuple[
            dict[str, Any], dict[str, Any] | None, dict[str, Any], str
        ]:
            async with OpenCodeClient(
                runtime.base_url,
                USERNAME,
                runtime.password,
                timeout=10,
            ) as client:
                health = await client.health()
                mcp = None
                if mcp_url:
                    mcp_config: dict[str, Any] = {
                        "type": "remote",
                        "url": mcp_url,
                        "enabled": True,
                        "oauth": False,
                    }
                    mcp_token = os.getenv("OPENCODE_E2E_MCP_TOKEN", "").strip()
                    if mcp_token:
                        mcp_config["headers"] = {"Authorization": f"Bearer {mcp_token}"}
                    mcp = await client.add_mcp("SKYCLOUD_E2E", mcp_config)
                mcp_status = await client.get_mcp_status()
                session_id = await client.create_session("SKYCloud OpenCode E2E")
                session = await client.get_session(session_id)
                assert session is not None
                return health, mcp, mcp_status, session_id

        health, mcp, mcp_status, session_id = asyncio.run(exercise())
        assert health.get("healthy", True) is not False
        assert mcp is None or isinstance(mcp, dict)
        assert isinstance(mcp_status, dict)
        assert session_id


@pytest.mark.skipif(
    not MODEL_E2E_ENABLED,
    reason="设置 RUN_OPENCODE_MODEL_E2E=1 才执行真实模型调用",
)
def test_expert_engine_streams_through_official_opencode_runtime(
    session,
    test_user,
    test_workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real ExpertEngine run reaches OpenCode and persists its answer."""

    chat = _chat_config()
    conversation = repository.create_conversation(
        session,
        test_workspace.id,
        test_user.id,
        "expert",
        "OpenCode 模型 E2E",
    )
    session.commit()
    conversation, run, history = service.prepare_run(
        session,
        test_workspace,
        test_user.id,
        conversation.id,
        "只输出 E2E_OK，不调用任何工具。",
        request_id="opencode-model-e2e",
    )
    runtime = runtime_service.get_or_create_runtime(
        session, test_workspace.id, test_user.id
    )
    session.commit()

    with _OpenCodeRuntime(
        chat=chat,
        runtime_id=int(runtime.id),
        user_id=int(test_user.id),
        workspace_id=int(test_workspace.id),
    ) as opencode:
        assert opencode.base_url is not None
        runtime.container_id = opencode.name
        runtime.status = "running"
        session.commit()
        monkeypatch.setattr(
            docker_service,
            "runtime_uses_configured_image",
            lambda _: True,
        )
        monkeypatch.setattr(
            docker_service,
            "get_runtime_base_url",
            lambda *_args, **_kwargs: opencode.base_url,
        )
        monkeypatch.setattr(docker_service, "setup_mcp", lambda *_args, **_kwargs: None)
        monkeypatch.setenv("OPENCODE_EXPERT_AGENT", "skycloud-e2e")

        async def collect() -> list[Any]:
            return [
                event
                async for event in service.stream_prepared_run(
                    session,
                    test_workspace,
                    test_user.id,
                    conversation,
                    run,
                    history,
                )
            ]

        events = asyncio.run(asyncio.wait_for(collect(), timeout=MODEL_TIMEOUT))

    session.refresh(run)
    assert run.status == "completed"
    message = repository.get_messages(session, conversation.id)[-1]
    assert message.status == "completed"
    assert "E2E_OK" in message.content
    assert any(event.type == "done" for event in events)
