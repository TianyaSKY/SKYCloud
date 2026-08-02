"""MCP/OpenCode token-refresh and runtime isolation regression tests."""

from unittest.mock import patch

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
