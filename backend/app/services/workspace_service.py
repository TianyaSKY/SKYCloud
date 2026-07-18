"""Workspace service — Docker container lifecycle management for opencode."""

import json
import os
import secrets
import time

import docker
from docker.errors import DockerException, NotFound as ContainerNotFound
from loguru import logger
from sqlalchemy import and_

from app.extensions import db
from app.exceptions import BusinessRuleError, ResourceNotFoundError, ServiceOperationError
from app.models.workspace import Workspace
from app.services.workspace_types import CreateWorkspaceCommand, McpConnectionResult, WorkspaceSummary


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

OPENCODE_IMAGE = os.getenv("OPENCODE_IMAGE", "skycloud/opencode-workspace:latest")
SKYCLOUD_DOCKER_NETWORK = os.getenv("SKYCLOUD_DOCKER_NETWORK", "skycloud_skycloud-network")
# Resource limits
WORKSPACE_MEM_LIMIT = os.getenv("WORKSPACE_MEM_LIMIT", "1g")
WORKSPACE_CPU_QUOTA = int(os.getenv("WORKSPACE_CPU_QUOTA", "100000"))  # 1 core
WORKSPACE_CPU_PERIOD = int(os.getenv("WORKSPACE_CPU_PERIOD", "100000"))
# Max workspaces per user
MAX_WORKSPACES_PER_USER = int(os.getenv("MAX_WORKSPACES_PER_USER", "3"))


def _get_docker_client() -> docker.DockerClient:
    """Create a Docker client. Supports both Unix socket and TCP."""
    docker_host = os.getenv("DOCKER_HOST")
    if docker_host:
        return docker.DockerClient(base_url=docker_host)
    return docker.from_env()


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def list_workspaces(user_id: int) -> list[WorkspaceSummary]:
    """Return all workspaces belonging to *user_id*."""
    rows = (
        db.session.query(Workspace)
        .filter(Workspace.user_id == user_id)
        .order_by(Workspace.created_at.desc())
        .all()
    )
    # Sync status from Docker for running containers
    result = []
    for ws in rows:
        _sync_status(ws)
        result.append(_to_summary(ws))
    return result


def get_workspace(workspace_id: int, user_id: int) -> Workspace | None:
    ws = (
        db.session.query(Workspace)
        .filter(and_(Workspace.id == workspace_id, Workspace.user_id == user_id))
        .first()
    )
    if ws:
        _sync_status(ws)
    return ws


def to_summary(ws: Workspace) -> WorkspaceSummary:
    """将 ORM 实体转换为明确的 API 输出类型。"""
    return _to_summary(ws)


def create_workspace(command: CreateWorkspaceCommand) -> Workspace:
    """Create a new workspace: persist DB row, then spin up Docker container."""
    user_id = command.user_id
    # Check limit
    count = (
        db.session.query(Workspace)
        .filter(Workspace.user_id == user_id)
        .count()
    )
    if count >= MAX_WORKSPACES_PER_USER:
        raise BusinessRuleError(
            f"每个用户最多创建 {MAX_WORKSPACES_PER_USER} 个工作区"
        )

    password = secrets.token_urlsafe(24)
    ws = Workspace(
        user_id=user_id,
        name=command.name,
        container_password=password,
        status="creating",
    )
    db.session.add(ws)
    db.session.commit()

    try:
        container = _start_container(ws)
        ws.container_id = container.id
        ws.status = "running"
    except DockerException as exc:
        logger.exception("启动工作区容器失败：workspace_id={} reason={}", ws.id, exc)
        ws.status = "error"
        ws.error_message = str(exc)[:500]
    db.session.commit()
    if ws.status == "running":
        _auto_setup_mcp_safe(ws.id, user_id)
    return ws


def start_workspace(workspace_id: int, user_id: int) -> Workspace:
    ws = get_workspace(workspace_id, user_id)
    if not ws:
        raise ResourceNotFoundError("工作区不存在")
    if ws.status == "running":
        # 已在运行也补一次 MCP 配置（幂等），确保 Token 刷新后可恢复
        _auto_setup_mcp_safe(workspace_id, user_id)
        return ws
    try:
        client = _get_docker_client()
        container = client.containers.get(ws.container_id)
        container.start()
        ws.status = "running"
        ws.error_message = None
    except ContainerNotFound:
        # Container was removed — recreate
        try:
            container = _start_container(ws)
            ws.container_id = container.id
            ws.status = "running"
            ws.error_message = None
        except DockerException as exc:
            logger.exception("重建工作区容器失败：workspace_id={} reason={}", ws.id, exc)
            ws.status = "error"
            ws.error_message = str(exc)[:500]
    except DockerException as exc:
        logger.exception("启动工作区容器失败：workspace_id={} reason={}", ws.id, exc)
        ws.status = "error"
        ws.error_message = str(exc)[:500]
    db.session.commit()
    if ws.status == "running":
        _auto_setup_mcp_safe(workspace_id, user_id)
    return ws


def stop_workspace(workspace_id: int, user_id: int) -> Workspace:
    ws = get_workspace(workspace_id, user_id)
    if not ws:
        raise ResourceNotFoundError("工作区不存在")
    if ws.status == "stopped":
        return ws
    try:
        client = _get_docker_client()
        container = client.containers.get(ws.container_id)
        container.stop(timeout=10)
        ws.status = "stopped"
    except ContainerNotFound:
        ws.status = "stopped"
    except DockerException as exc:
        logger.exception("停止工作区容器失败：workspace_id={} reason={}", ws.id, exc)
        ws.status = "error"
        ws.error_message = str(exc)[:500]
    db.session.commit()
    return ws


def delete_workspace(workspace_id: int, user_id: int) -> None:
    ws = get_workspace(workspace_id, user_id)
    if not ws:
        raise ResourceNotFoundError("工作区不存在")
    # Remove container
    try:
        client = _get_docker_client()
        container = client.containers.get(ws.container_id)
        container.remove(force=True, v=True)
    except (ContainerNotFound, DockerException):
        pass
    except DockerException as exc:
        logger.exception("删除工作区容器失败：workspace_id={} reason={}", ws.id, exc)
    db.session.delete(ws)
    db.session.commit()


def get_container_url(ws: Workspace) -> str:
    """Return the internal URL for the opencode container.

    Inside Docker network we use the container name as hostname.
    Outside Docker (local dev on Windows) we use localhost + mapped port
    because WSL2 container IPs are not routable from the Windows host.
    """
    # Detect if we are inside a Docker container
    in_docker = os.path.exists("/.dockerenv")

    if in_docker:
        # Inside Docker network, containers communicate by name
        container_name = _container_name(ws)
        return f"http://{container_name}:3000"

    # Local dev — use the mapped port on localhost
    try:
        client = _get_docker_client()
        container = client.containers.get(ws.container_id)
        ports = container.attrs.get("NetworkSettings", {}).get("Ports", {})
        mapping = ports.get("3000/tcp")
        if mapping and len(mapping) > 0:
            host_port = mapping[0].get("HostPort")
            if host_port:
                return f"http://127.0.0.1:{host_port}"
    except DockerException as exc:
        logger.warning("读取工作区端口映射失败：workspace_id={} reason={}", ws.id, exc)

    # Fallback: try container name anyway
    container_name = _container_name(ws)
    return f"http://{container_name}:3000"


def _get_access_url(ws: Workspace) -> str | None:
    """Return the browser-accessible URL for the opencode container.

    This reads the Docker port mapping to return http://localhost:{host_port}/
    which the frontend can use directly in an iframe.
    """
    if not ws.container_id:
        return None
    try:
        client = _get_docker_client()
        container = client.containers.get(ws.container_id)
        ports = container.attrs.get("NetworkSettings", {}).get("Ports", {})
        mapping = ports.get("3000/tcp")
        if mapping and len(mapping) > 0:
            host_port = mapping[0].get("HostPort")
            if host_port:
                return f"http://localhost:{host_port}"
    except DockerException as exc:
        logger.warning("读取工作区访问地址失败：workspace_id={} reason={}", ws.id, exc)
    return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _container_name(ws: Workspace) -> str:
    return f"skycloud-workspace-{ws.id}"


def _to_summary(ws: Workspace) -> WorkspaceSummary:
    payload = ws.to_dict()
    return WorkspaceSummary(
        **payload,
        access_url=_get_access_url(ws) if ws.status == "running" else None,
    )


def _start_container(ws: Workspace) -> "docker.models.containers.Container":
    """docker run the opencode image for this workspace."""
    client = _get_docker_client()
    name = _container_name(ws)

    # Remove existing container with the same name (if any)
    try:
        old = client.containers.get(name)
        old.remove(force=True)
    except (ContainerNotFound, DockerException):
        pass

    run_kwargs = dict(
        image=OPENCODE_IMAGE,
        name=name,
        detach=True,
        environment={
            "SKYCLOUD_WORKSPACE_ID": str(ws.id),
        },
        # Resource limits
        mem_limit=WORKSPACE_MEM_LIMIT,
        cpu_quota=WORKSPACE_CPU_QUOTA,
        cpu_period=WORKSPACE_CPU_PERIOD,
        # Networking — join the SKYCloud network so the API container can reach it
        network=SKYCLOUD_DOCKER_NETWORK,
        # Restart policy
        restart_policy={"Name": "unless-stopped"},
        # Always map port so frontend can access directly via localhost
        ports={"3000/tcp": None},  # None = Docker assigns a random host port
    )

    container = client.containers.run(**run_kwargs)
    logger.info("工作区容器已启动：{} ({})", name, container.short_id)
    return container


def _sync_status(ws: Workspace) -> None:
    """Best-effort sync of DB status with actual Docker container state."""
    if not ws.container_id:
        return
    try:
        client = _get_docker_client()
        container = client.containers.get(ws.container_id)
        docker_status = container.status  # running | exited | paused | ...
        if docker_status == "running":
            if ws.status != "running":
                ws.status = "running"
                db.session.commit()
        elif docker_status in ("exited", "dead"):
            if ws.status not in ("stopped", "error"):
                ws.status = "stopped"
                db.session.commit()
    except ContainerNotFound:
        if ws.status not in ("stopped", "error", "creating"):
            ws.status = "stopped"
            db.session.commit()
    except DockerException as exc:
        logger.warning("同步工作区状态失败：workspace_id={} reason={}", ws.id, exc)


# ---------------------------------------------------------------------------
# Restart
# ---------------------------------------------------------------------------


def restart_workspace(workspace_id: int, user_id: int) -> Workspace:
    """Restart a workspace container (docker restart)."""
    ws = get_workspace(workspace_id, user_id)
    if not ws:
        raise ResourceNotFoundError("工作区不存在")
    if not ws.container_id:
        raise BusinessRuleError("工作区容器不存在，请删除后重新创建")
    try:
        client = _get_docker_client()
        container = client.containers.get(ws.container_id)
        container.restart(timeout=3)
        ws.status = "running"
        ws.error_message = None
    except ContainerNotFound:
        # Container was removed — recreate
        try:
            container = _start_container(ws)
            ws.container_id = container.id
            ws.status = "running"
            ws.error_message = None
        except DockerException as exc:
            logger.exception("重建工作区容器失败：workspace_id={} reason={}", ws.id, exc)
            ws.status = "error"
            ws.error_message = str(exc)[:500]
    except DockerException as exc:
        logger.exception("重启工作区容器失败：workspace_id={} reason={}", ws.id, exc)
        ws.status = "error"
        ws.error_message = str(exc)[:500]
    db.session.commit()
    if ws.status == "running":
        _auto_setup_mcp_safe(workspace_id, user_id)
    return ws


# ---------------------------------------------------------------------------
# Auto-connect MCP
# ---------------------------------------------------------------------------

# MCP server port visible from inside the workspace container.
# In Docker, the MCP container name is skycloud-backend-mcp and it listens
# on port 5001. The workspace container connects via host.docker.internal
# which resolves to the Docker host — matching the port mapping in
# docker-compose.yml.
_MCP_EXTERNAL_PORT = int(os.getenv("MCP_PORT", "5001"))

# Config file path inside the opencode workspace container
_OPENCODE_CONFIG_PATH = "/root/.config/opencode/opencode.json"


def _mcp_url_for_container() -> str:
    in_docker = os.path.exists("/.dockerenv")
    if in_docker:
        return f"http://skycloud-backend-mcp:{_MCP_EXTERNAL_PORT}/mcp"
    return f"http://host.docker.internal:{_MCP_EXTERNAL_PORT}/mcp"


def _write_mcp_config_to_container(container, mcp_url: str, mcp_token: str) -> None:
    """将用户唯一 MCP Token 写入工作区 opencode.json。"""
    opencode_config = {
        "$schema": "https://opencode.ai/config.json",
        "mcp": {
            "SKYCLOUD": {
                "type": "remote",
                "url": mcp_url,
                "enabled": True,
                "oauth": False,
                "headers": {
                    "Authorization": f"Bearer {mcp_token}",
                },
            }
        },
    }
    config_json = json.dumps(opencode_config, indent=2, ensure_ascii=False)

    container.exec_run(
        cmd=["mkdir", "-p", "/root/.config/opencode"],
        user="root",
    )
    escaped_json = config_json.replace("'", "'\\''")
    container.exec_run(
        cmd=["sh", "-c", f"echo '{escaped_json}' > {_OPENCODE_CONFIG_PATH}"],
        user="root",
    )
    exit_code, _output = container.exec_run(
        cmd=["cat", _OPENCODE_CONFIG_PATH],
        user="root",
    )
    if exit_code != 0:
        raise RuntimeError("Failed to verify config file in container")


def setup_mcp_connection(workspace_id: int, user_id: int) -> McpConnectionResult:
    """使用用户唯一 MCP Token 写入工作区 opencode.json（不新建 Token）。"""
    from app.services import mcp_token_service

    ws = get_workspace(workspace_id, user_id)
    if not ws:
        raise ResourceNotFoundError("工作区不存在")
    if ws.status != "running":
        raise BusinessRuleError("工作区必须处于运行状态才能配置 MCP 连接")
    if not ws.container_id:
        raise BusinessRuleError("工作区容器不存在")

    token_record, mcp_token = mcp_token_service.ensure_user_mcp_token(user_id)
    mcp_url = _mcp_url_for_container()

    try:
        client = _get_docker_client()
        container = client.containers.get(ws.container_id)
        _write_mcp_config_to_container(container, mcp_url, mcp_token)

        logger.info(
            "工作区 MCP 连接已配置：workspace_id={}, user_id={}, token_id={}",
            workspace_id,
            user_id,
            token_record.id,
        )
        return McpConnectionResult(
            mcp_url=mcp_url,
            token_id=token_record.id,
            config_path=_OPENCODE_CONFIG_PATH,
        )
    except ContainerNotFound:
        raise BusinessRuleError("工作区容器不存在")
    except DockerException as exc:
        logger.exception("配置工作区 MCP 连接失败：workspace_id={}", workspace_id)
        raise ServiceOperationError("配置 MCP 连接失败") from exc


def _auto_setup_mcp_safe(workspace_id: int, user_id: int) -> None:
    """工作区就绪后自动注入 MCP；失败只记日志，不阻断生命周期。"""
    try:
        setup_mcp_connection(workspace_id, user_id)
    except Exception:
        logger.exception(
            "工作区自动配置 MCP 失败：workspace_id={}, user_id={}",
            workspace_id,
            user_id,
        )


def resync_mcp_for_user(user_id: int) -> int:
    """将当前唯一 MCP Token 同步到用户所有运行中的工作区。返回成功数。"""
    rows = (
        db.session.query(Workspace)
        .filter(Workspace.user_id == user_id, Workspace.status == "running")
        .all()
    )
    ok = 0
    for ws in rows:
        try:
            setup_mcp_connection(ws.id, user_id)
            ok += 1
        except Exception:
            logger.exception(
                "同步 MCP 到工作区失败：workspace_id={}, user_id={}",
                ws.id,
                user_id,
            )
    return ok
