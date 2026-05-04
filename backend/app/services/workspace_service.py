"""Workspace service — Docker container lifecycle management for opencode."""

import logging
import os
import secrets

import docker
from docker.errors import DockerException, NotFound as ContainerNotFound
from sqlalchemy import and_

from app.extensions import db
from app.models.workspace import Workspace

logger = logging.getLogger(__name__)

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


def list_workspaces(user_id: int) -> list[dict]:
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
        d = ws.to_dict()
        d["access_url"] = _get_access_url(ws) if ws.status == "running" else None
        result.append(d)
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


def create_workspace(user_id: int, name: str) -> Workspace:
    """Create a new workspace: persist DB row, then spin up Docker container."""
    # Check limit
    count = (
        db.session.query(Workspace)
        .filter(Workspace.user_id == user_id)
        .count()
    )
    if count >= MAX_WORKSPACES_PER_USER:
        raise ValueError(
            f"每个用户最多创建 {MAX_WORKSPACES_PER_USER} 个工作区"
        )

    password = secrets.token_urlsafe(24)
    ws = Workspace(
        user_id=user_id,
        name=name or "My Workspace",
        container_password=password,
        status="creating",
    )
    db.session.add(ws)
    db.session.commit()

    try:
        container = _start_container(ws)
        ws.container_id = container.id
        ws.status = "running"
    except Exception as exc:
        logger.exception("Failed to start workspace container")
        ws.status = "error"
        ws.error_message = str(exc)[:500]
    db.session.commit()
    return ws


def start_workspace(workspace_id: int, user_id: int) -> Workspace:
    ws = get_workspace(workspace_id, user_id)
    if not ws:
        raise FileNotFoundError("工作区不存在")
    if ws.status == "running":
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
        except Exception as exc:
            ws.status = "error"
            ws.error_message = str(exc)[:500]
    except Exception as exc:
        ws.status = "error"
        ws.error_message = str(exc)[:500]
    db.session.commit()
    return ws


def stop_workspace(workspace_id: int, user_id: int) -> Workspace:
    ws = get_workspace(workspace_id, user_id)
    if not ws:
        raise FileNotFoundError("工作区不存在")
    if ws.status == "stopped":
        return ws
    try:
        client = _get_docker_client()
        container = client.containers.get(ws.container_id)
        container.stop(timeout=10)
        ws.status = "stopped"
    except ContainerNotFound:
        ws.status = "stopped"
    except Exception as exc:
        ws.status = "error"
        ws.error_message = str(exc)[:500]
    db.session.commit()
    return ws


def delete_workspace(workspace_id: int, user_id: int) -> None:
    ws = get_workspace(workspace_id, user_id)
    if not ws:
        raise FileNotFoundError("工作区不存在")
    # Remove container
    try:
        client = _get_docker_client()
        container = client.containers.get(ws.container_id)
        container.remove(force=True, v=True)
    except (ContainerNotFound, DockerException):
        pass
    except Exception:
        logger.exception("Failed to remove container %s", ws.container_id)
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
    except Exception:
        pass

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
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _container_name(ws: Workspace) -> str:
    return f"skycloud-workspace-{ws.id}"


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
        environment={},
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
    logger.info("Started workspace container %s (%s)", name, container.short_id)
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
    except Exception:
        pass
