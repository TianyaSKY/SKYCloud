"""MCP/OpenCode 令牌刷新与运行时隔离回归测试."""

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock, patch

import jwt
import pytest
from sqlalchemy.orm import Session
from runtime_common.contracts import ContainerPayload

from app.features.workspace import docker_service
from app.features.workspace import service as workspace_service
from app.infra.extensions import SECRET_KEY
from app.mcp.runtime_token_service import (
    get_active_runtime_token,
    issue_runtime_token,
    validate_runtime_token,
)
from app.models.opencode_runtime import OpenCodeRuntime
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember


def test_local_api_uses_authenticated_controller_relay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_workspace = cast(Workspace, SimpleNamespace(id=12))
    runtime = cast(
        OpenCodeRuntime, SimpleNamespace(id=7, container_id="runtime-container")
    )
    monkeypatch.delenv("OPENCODE_RUNTIME_BASE_URL", raising=False)
    monkeypatch.delenv("OPENCODE_SERVER_URL", raising=False)
    monkeypatch.setenv("RUNTIME_CONTROLLER_URL", "http://127.0.0.1:5003")

    with patch(
        "app.features.workspace.docker_service.get_access_url",
        return_value="http://127.0.0.1:5003/runtimes/runtime-container/http",
    ):
        assert (
            docker_service.get_runtime_base_url(fake_workspace, 8, runtime)
            == "http://127.0.0.1:5003/runtimes/runtime-container/http"
        )


def test_containerized_api_uses_authenticated_controller_relay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_workspace = cast(Workspace, SimpleNamespace(id=12))
    runtime = cast(
        OpenCodeRuntime, SimpleNamespace(id=7, container_id="runtime-container")
    )
    monkeypatch.delenv("OPENCODE_RUNTIME_BASE_URL", raising=False)
    monkeypatch.delenv("OPENCODE_SERVER_URL", raising=False)
    monkeypatch.setenv(
        "RUNTIME_CONTROLLER_URL", "http://runtime-controller-control:5003"
    )

    assert (
        docker_service.get_runtime_base_url(fake_workspace, 8, runtime)
        == "http://runtime-controller-control:5003/runtimes/runtime-container/http"
    )


def test_local_api_runtime_uses_isolated_mcp_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENCODE_MCP_URL", raising=False)
    monkeypatch.setenv("RUNTIME_CONTROLLER_URL", "http://127.0.0.1:5003")

    assert (
        docker_service._mcp_endpoint()  # pyright: ignore[reportPrivateUsage]
        == f"http://skycloud-backend-mcp:{docker_service.MCP_PORT}/mcp"
    )


def test_old_runtime_image_is_marked_for_recreation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        docker_service,
        "OPENCODE_IMAGE",
        "ghcr.io/anomalyco/opencode:latest",
    )
    current_policy = docker_service.POLICY_VERSION
    old_container: dict[str, Any] = {
        "id": "a" * 64,
        "name": "old",
        "status": "stopped",
        "image": "skycloud/opencode-workspace:latest",
        "labels": {"skycloud.runtime_policy": current_policy},
        "ports": {},
    }
    current_container: dict[str, Any] = {
        "id": "b" * 64,
        "name": "current",
        "status": "running",
        "image": "ghcr.io/anomalyco/opencode:latest",
        "labels": {"skycloud.runtime_policy": current_policy},
        "ports": {},
    }

    assert not docker_service._container_uses_configured_image(  # pyright: ignore[reportPrivateUsage]
        cast(ContainerPayload, old_container)
    )
    assert docker_service._container_uses_configured_image(  # pyright: ignore[reportPrivateUsage]
        cast(ContainerPayload, current_container)
    )


def test_runtime_image_check_detects_a_stale_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = cast(
        OpenCodeRuntime, SimpleNamespace(id=7, container_id="runtime-container")
    )
    client = Mock()
    client.inspect.return_value = {
        "id": "a" * 64,
        "name": "runtime",
        "status": "running",
        "image": "skycloud/opencode-workspace:latest",
        "labels": {"skycloud.runtime_policy": docker_service.POLICY_VERSION},
        "ports": {},
    }
    monkeypatch.setattr(
        docker_service,
        "OPENCODE_IMAGE",
        "ghcr.io/anomalyco/opencode:latest",
    )
    monkeypatch.setattr(docker_service, "_client", lambda: client)

    assert not docker_service.runtime_uses_configured_image(runtime)


def test_opencode_mcp_config_merge_preserves_existing_settings() -> None:
    existing: dict[str, Any] = {
        "model": "openai/gpt-5",
        "agent": {"default": {"model": "openai/gpt-5"}},
        "mcp": {"OTHER": {"type": "local", "command": ["other"]}},
    }
    merged: dict[str, Any] = cast(
        dict[str, Any],
        docker_service.merge_opencode_mcp_config(
            existing,
            {
                "type": "remote",
                "url": "http://mcp/mcp",
                "enabled": True,
            },
        ),
    )

    assert merged["model"] == existing["model"]
    assert merged["agent"] == existing["agent"]
    assert merged["mcp"]["OTHER"] == existing["mcp"]["OTHER"]
    assert merged["mcp"]["SKYCLOUD"]["url"] == "http://mcp/mcp"


def test_opencode_chat_provider_uses_existing_chat_api_config() -> None:
    existing: dict[str, Any] = {
        "provider": {"other": {"npm": "other-provider"}},
        "model": "other/model",
    }
    chat_config: dict[str, Any] = {
        "base_url": "https://chat.example/v1",
        "model": "openai/tool-model",
        "api_key": "test-key",
    }

    merged: dict[str, Any] = cast(
        dict[str, Any],
        docker_service.merge_opencode_chat_provider(existing, chat_config),
    )

    assert merged["provider"]["other"] == existing["provider"]["other"]
    provider: dict[str, Any] = merged["provider"][
        docker_service.OPENCODE_CHAT_PROVIDER_ID
    ]
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["options"] == {
        "baseURL": "https://chat.example/v1",
        "apiKey": "test-key",
    }
    assert (
        provider["models"]["openai/tool-model"]["name"] == "openai/tool-model"
    )
    assert merged["model"] == "skycloud-chat/openai/tool-model"


def test_chat_provider_change_requires_runtime_reload() -> None:
    chat_config: dict[str, Any] = {
        "base_url": "https://chat.example/v1",
        "model": "tool-model",
        "api_key": "test-key",
    }
    configured = docker_service.merge_opencode_chat_provider({}, chat_config)

    assert docker_service.opencode_chat_provider_needs_reload({}, chat_config)
    assert not docker_service.opencode_chat_provider_needs_reload(
        configured,
        chat_config,
    )


def test_runtime_token_is_bound_to_runtime_and_revocable(
    session: Session, test_user: User, test_workspace: Workspace
) -> None:
    runtime = OpenCodeRuntime(
        workspace_id=int(test_workspace.id),
        user_id=int(test_user.id),
        status="running",
    )
    session.add(runtime)
    session.flush()

    record, raw = issue_runtime_token(
        session, runtime, role="admin", ttl_seconds=300
    )
    claims: dict[str, Any] = jwt.decode(raw, SECRET_KEY, algorithms=["HS256"])
    assert claims["type"] == "mcp_runtime"
    assert claims["workspace_id"] == test_workspace.id
    assert claims["runtime_id"] == runtime.id
    assert claims["sub"] == str(test_user.id)
    active_token = get_active_runtime_token(session, raw)
    assert active_token is not None
    assert active_token.id == record.id
    assert validate_runtime_token(session, raw) is not None

    runtime.status = "stopped"
    session.flush()
    assert validate_runtime_token(session, raw) is None


def test_role_change_refreshes_a_running_runtime_token(
    session: Session, test_user: User, test_workspace: Workspace
) -> None:
    member_user = User(username="runtime-member", role="common")
    member_user.set_password("password")
    session.add(member_user)
    session.flush()
    session.add(
        WorkspaceMember(
            workspace_id=int(test_workspace.id),
            user_id=int(member_user.id),
            role="viewer",
        )
    )
    runtime = OpenCodeRuntime(
        workspace_id=int(test_workspace.id),
        user_id=int(member_user.id),
        container_id="container-1",
        status="running",
    )
    session.add(runtime)
    session.flush()
    record, old_token = issue_runtime_token(session, runtime, role="viewer")

    with patch("app.features.workspace.docker_service.setup_mcp") as setup_mcp:
        member = workspace_service.update_member_role(
            session,
            workspace_id=int(test_workspace.id),
            actor_id=int(test_user.id),
            target_user_id=int(member_user.id),
            new_role="editor",
        )

    assert member.role == "editor"
    assert record.is_revoked
    assert get_active_runtime_token(session, old_token) is None
    setup_mcp.assert_called_once()
