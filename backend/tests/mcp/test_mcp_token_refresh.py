"""MCP/OpenCode token-refresh and runtime isolation regression tests."""

from unittest.mock import Mock, patch
from types import SimpleNamespace

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
from app.models.workspace import WorkspaceMember
import jwt


def test_runtime_container_name_is_unique_per_user_and_workspace():
    class Workspace:
        id = 12

    assert docker_service._name(Workspace(), 8) == "skycloud-opencode-w12-u8"
    assert docker_service._name(Workspace(), 19) == "skycloud-opencode-w12-u19"


def test_local_api_uses_runtime_loopback_port(monkeypatch):
    class Workspace:
        id = 12

    runtime = SimpleNamespace(id=7, container_id="runtime-container")
    monkeypatch.delenv("OPENCODE_RUNTIME_BASE_URL", raising=False)
    monkeypatch.delenv("OPENCODE_SERVER_URL", raising=False)
    monkeypatch.setattr(docker_service.os.path, "exists", lambda _path: False)

    with patch(
        "app.features.workspace.docker_service.get_access_url",
        return_value="http://localhost:39123",
    ):
        assert (
            docker_service.get_runtime_base_url(Workspace(), 8, runtime)
            == "http://localhost:39123"
        )


def test_containerized_api_uses_runtime_docker_dns(monkeypatch):
    class Workspace:
        id = 12

    runtime = SimpleNamespace(id=7, container_id="runtime-container")
    monkeypatch.delenv("OPENCODE_RUNTIME_BASE_URL", raising=False)
    monkeypatch.delenv("OPENCODE_SERVER_URL", raising=False)
    monkeypatch.setattr(docker_service.os.path, "exists", lambda _path: True)

    assert (
        docker_service.get_runtime_base_url(Workspace(), 8, runtime)
        == "http://skycloud-opencode-w12-u8:3000"
    )


def test_local_api_runtime_uses_host_docker_internal_for_mcp(monkeypatch):
    monkeypatch.delenv("OPENCODE_MCP_URL", raising=False)
    monkeypatch.setattr(docker_service.os.path, "exists", lambda _path: False)

    assert docker_service._mcp_endpoint() == f"http://host.docker.internal:{docker_service.MCP_PORT}/mcp"


def test_missing_official_image_is_pulled_automatically(monkeypatch):
    image = "ghcr.io/anomalyco/opencode:latest"
    client = Mock()
    client.images.get.side_effect = docker_service.ImageNotFound("not found")
    monkeypatch.setattr(docker_service, "OPENCODE_IMAGE", image)

    docker_service._ensure_opencode_image(client)

    client.images.get.assert_called_once_with(image)
    client.images.pull.assert_called_once_with(image)


def test_old_runtime_image_is_marked_for_recreation(monkeypatch):
    monkeypatch.setattr(
        docker_service,
        "OPENCODE_IMAGE",
        "ghcr.io/anomalyco/opencode:latest",
    )
    old_container = SimpleNamespace(
        attrs={"Config": {"Image": "skycloud/opencode-workspace:latest"}}
    )
    current_container = SimpleNamespace(
        attrs={"Config": {"Image": "ghcr.io/anomalyco/opencode:latest"}}
    )

    assert not docker_service._container_uses_configured_image(old_container)
    assert docker_service._container_uses_configured_image(current_container)


def test_runtime_image_check_detects_a_stale_container(monkeypatch):
    runtime = SimpleNamespace(id=7, container_id="runtime-container")
    client = Mock()
    client.containers.get.return_value = SimpleNamespace(
        attrs={"Config": {"Image": "skycloud/opencode-workspace:latest"}}
    )
    monkeypatch.setattr(
        docker_service,
        "OPENCODE_IMAGE",
        "ghcr.io/anomalyco/opencode:latest",
    )
    monkeypatch.setattr(docker_service, "_client", lambda: client)

    assert not docker_service.runtime_uses_configured_image(runtime)


def test_opencode_mcp_config_merge_preserves_existing_settings():
    existing = {
        "model": "openai/gpt-5",
        "agent": {"default": {"model": "openai/gpt-5"}},
        "mcp": {"OTHER": {"type": "local", "command": ["other"]}},
    }
    merged = docker_service.merge_opencode_mcp_config(
        existing,
        {
            "type": "remote",
            "url": "http://mcp/mcp",
            "enabled": True,
        },
    )

    assert merged["model"] == existing["model"]
    assert merged["agent"] == existing["agent"]
    assert merged["mcp"]["OTHER"] == existing["mcp"]["OTHER"]
    assert merged["mcp"]["SKYCLOUD"]["url"] == "http://mcp/mcp"


def test_opencode_chat_provider_uses_existing_chat_api_config():
    existing = {
        "provider": {"other": {"npm": "other-provider"}},
        "model": "other/model",
    }
    chat_config = {
        "base_url": "https://chat.example/v1",
        "model": "openai/tool-model",
        "api_key": "test-key",
    }

    merged = docker_service.merge_opencode_chat_provider(existing, chat_config)

    assert merged["provider"]["other"] == existing["provider"]["other"]
    provider = merged["provider"][docker_service.OPENCODE_CHAT_PROVIDER_ID]
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["options"] == {
        "baseURL": "https://chat.example/v1",
        "apiKey": "test-key",
    }
    assert provider["models"]["openai/tool-model"]["name"] == "openai/tool-model"
    assert merged["model"] == "skycloud-chat/openai/tool-model"


def test_chat_provider_change_requires_runtime_reload():
    chat_config = {
        "base_url": "https://chat.example/v1",
        "model": "tool-model",
        "api_key": "test-key",
    }
    configured = docker_service.merge_opencode_chat_provider({}, chat_config)

    assert docker_service._opencode_chat_provider_needs_reload({}, chat_config)
    assert not docker_service._opencode_chat_provider_needs_reload(
        configured,
        chat_config,
    )


def test_runtime_token_is_bound_to_runtime_and_revocable(
        session, test_user, test_workspace
):
    runtime = OpenCodeRuntime(
        workspace_id=test_workspace.id,
        user_id=test_user.id,
        status="running",
    )
    session.add(runtime)
    session.flush()

    record, raw = issue_runtime_token(session, runtime, role="admin", ttl_seconds=300)
    claims = jwt.decode(raw, SECRET_KEY, algorithms=["HS256"])
    assert claims["type"] == "mcp_runtime"
    assert claims["workspace_id"] == test_workspace.id
    assert claims["runtime_id"] == runtime.id
    assert claims["sub"] == str(test_user.id)
    assert get_active_runtime_token(session, raw).id == record.id
    assert validate_runtime_token(session, raw) is not None

    runtime.status = "stopped"
    session.flush()
    assert validate_runtime_token(session, raw) is None


def test_role_change_refreshes_a_running_runtime_token(
        session, test_user, test_workspace
):
    member_user = User(username="runtime-member", role="common")
    member_user.set_password("password")
    session.add(member_user)
    session.flush()
    session.add(
        WorkspaceMember(
            workspace_id=test_workspace.id,
            user_id=member_user.id,
            role="viewer",
        )
    )
    runtime = OpenCodeRuntime(
        workspace_id=test_workspace.id,
        user_id=member_user.id,
        container_id="container-1",
        status="running",
    )
    session.add(runtime)
    session.flush()
    record, old_token = issue_runtime_token(session, runtime, role="viewer")

    with patch("app.features.workspace.docker_service.setup_mcp") as setup_mcp:
        member = workspace_service.update_member_role(
            session,
            workspace_id=test_workspace.id,
            actor_id=test_user.id,
            target_user_id=member_user.id,
            new_role="editor",
        )

    assert member.role == "editor"
    assert record.is_revoked
    assert get_active_runtime_token(session, old_token) is None
    setup_mcp.assert_called_once()
