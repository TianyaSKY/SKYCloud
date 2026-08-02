"""工作空间路由：空间 CRUD 与成员管理。"""

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user
from app.infra.extensions import get_db
from app.models.user import User
from app.features.workspace import service as workspace_service
from app.features.workspace.schemas import (
    WorkspaceCreateRequest,
    WorkspaceUpdateRequest,
    MemberInviteRequest,
    MemberRoleUpdateRequest,
)
from app.features.workspace.permissions import assert_member, assert_admin
from app.features.workspace.permissions import assert_can_write
from app.features.workspace import docker_service

router = APIRouter(tags=["workspace"])


# ---------------------------------------------------------------------------
# 工作空间 CRUD
# ---------------------------------------------------------------------------


@router.post("/workspace")
async def create_workspace(
        payload: WorkspaceCreateRequest,
        current_user: User = Depends(get_current_user),
        session: Session = Depends(get_db),
):
    """创建新的工作空间。"""
    workspace = workspace_service.create_workspace(
        session,
        owner_id=current_user.id,
        name=payload.name,
        description=payload.description,
    )
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content=workspace.to_dict(),
    )


@router.get("/workspace")
async def list_workspaces(
        current_user: User = Depends(get_current_user),
        session: Session = Depends(get_db),
):
    """列出当前用户加入的所有工作空间。"""
    workspaces = workspace_service.get_user_workspaces(session, current_user.id)
    for workspace in workspaces:
        entity = workspace_service.get_workspace(session, workspace["id"])
        workspace.update(docker_service.summary(session, entity, current_user.id))
    return {"workspaces": workspaces, "code": 200}


@router.get("/workspace/{workspace_id}")
async def get_workspace(
        workspace_id: int,
        current_user: User = Depends(get_current_user),
        session: Session = Depends(get_db),
):
    """获取工作空间详情（含成员列表）。"""
    # 校验成员身份
    assert_member(session, workspace_id, current_user.id)
    detail = workspace_service.get_workspace_detail(session, workspace_id, current_user.id)
    detail.update(
        docker_service.summary(
            session,
            workspace_service.get_workspace(session, workspace_id),
            current_user.id,
        )
    )
    return detail


# ---------------------------------------------------------------------------
# OpenCode Docker 工作区
# ---------------------------------------------------------------------------


def _docker_response(session: Session, workspace_id: int, user_id: int | None = None) -> dict:
    workspace = workspace_service.get_workspace(session, workspace_id)
    return {
        **workspace.to_dict(),
        **docker_service.summary(session, workspace, user_id),
    }


@router.post("/workspace/{workspace_id}/opencode/start")
async def start_opencode_workspace(
        workspace_id: int,
        current_user: User = Depends(get_current_user),
        session: Session = Depends(get_db),
):
    """启动当前协作空间的 OpenCode 容器；编辑者及以上可操作。"""
    assert_can_write(session, workspace_id, current_user.id)
    workspace = workspace_service.get_workspace(session, workspace_id)
    docker_service.start(session, workspace, current_user.id)
    return _docker_response(session, workspace_id, current_user.id)


@router.post("/workspace/{workspace_id}/opencode/stop")
async def stop_opencode_workspace(
        workspace_id: int,
        current_user: User = Depends(get_current_user),
        session: Session = Depends(get_db),
):
    """停止当前协作空间的 OpenCode 容器。"""
    assert_can_write(session, workspace_id, current_user.id)
    workspace = workspace_service.get_workspace(session, workspace_id)
    docker_service.stop(session, workspace, current_user.id)
    return _docker_response(session, workspace_id, current_user.id)


@router.post("/workspace/{workspace_id}/opencode/restart")
async def restart_opencode_workspace(
        workspace_id: int,
        current_user: User = Depends(get_current_user),
        session: Session = Depends(get_db),
):
    """重启当前协作空间的 OpenCode 容器并刷新 MCP 配置。"""
    assert_can_write(session, workspace_id, current_user.id)
    workspace = workspace_service.get_workspace(session, workspace_id)
    docker_service.restart(session, workspace, current_user.id)
    return _docker_response(session, workspace_id, current_user.id)


@router.post("/workspace/{workspace_id}/opencode/setup-mcp")
async def setup_opencode_mcp(
        workspace_id: int,
        current_user: User = Depends(get_current_user),
        session: Session = Depends(get_db),
):
    """重新写入当前用户的 MCP Token，用于 Token 刷新后的修复。"""
    assert_can_write(session, workspace_id, current_user.id)
    workspace = workspace_service.get_workspace(session, workspace_id)
    docker_service.setup_mcp(session, workspace, current_user.id)
    return {"success": True, **_docker_response(session, workspace_id, current_user.id)}


# ---------------------------------------------------------------------------
# Per-user OpenCode Runtime API
# ---------------------------------------------------------------------------


@router.get("/workspace/{workspace_id}/opencode/runtime")
async def get_opencode_runtime(
        workspace_id: int,
        current_user: User = Depends(get_current_user),
        session: Session = Depends(get_db),
):
    """Return only the current member's OpenCode Runtime status."""
    assert_member(session, workspace_id, current_user.id)
    workspace = workspace_service.get_workspace(session, workspace_id)
    return _docker_response(session, workspace_id, current_user.id)


@router.post("/workspace/{workspace_id}/opencode/runtime/start")
async def start_opencode_runtime(
        workspace_id: int,
        current_user: User = Depends(get_current_user),
        session: Session = Depends(get_db),
):
    assert_can_write(session, workspace_id, current_user.id)
    workspace = workspace_service.get_workspace(session, workspace_id)
    docker_service.start(session, workspace, current_user.id)
    return _docker_response(session, workspace_id, current_user.id)


@router.post("/workspace/{workspace_id}/opencode/runtime/stop")
async def stop_opencode_runtime(
        workspace_id: int,
        current_user: User = Depends(get_current_user),
        session: Session = Depends(get_db),
):
    assert_can_write(session, workspace_id, current_user.id)
    workspace = workspace_service.get_workspace(session, workspace_id)
    docker_service.stop(session, workspace, current_user.id)
    return _docker_response(session, workspace_id, current_user.id)


@router.post("/workspace/{workspace_id}/opencode/runtime/restart")
async def restart_opencode_runtime(
        workspace_id: int,
        current_user: User = Depends(get_current_user),
        session: Session = Depends(get_db),
):
    assert_can_write(session, workspace_id, current_user.id)
    workspace = workspace_service.get_workspace(session, workspace_id)
    docker_service.restart(session, workspace, current_user.id)
    return _docker_response(session, workspace_id, current_user.id)


@router.put("/workspace/{workspace_id}")
async def update_workspace(
        workspace_id: int,
        payload: WorkspaceUpdateRequest,
        current_user: User = Depends(get_current_user),
        session: Session = Depends(get_db),
):
    """更新工作空间信息（仅 admin）。"""
    workspace = workspace_service.update_workspace(
        session,
        workspace_id=workspace_id,
        user_id=current_user.id,
        name=payload.name,
        description=payload.description,
    )
    return workspace.to_dict()


@router.delete("/workspace/{workspace_id}")
async def delete_workspace(
        workspace_id: int,
        current_user: User = Depends(get_current_user),
        session: Session = Depends(get_db),
):
    """删除工作空间（仅 owner）。"""
    workspace_service.delete_workspace(session, workspace_id, current_user.id)
    return JSONResponse(status_code=200, content={"message": "已删除", "code": 200})


# ---------------------------------------------------------------------------
# 成员管理
# ---------------------------------------------------------------------------


@router.post("/workspace/{workspace_id}/members")
async def invite_member(
        workspace_id: int,
        payload: MemberInviteRequest,
        current_user: User = Depends(get_current_user),
        session: Session = Depends(get_db),
):
    """邀请成员加入工作空间（仅 admin）。"""
    member = workspace_service.invite_member(
        session,
        workspace_id=workspace_id,
        actor_id=current_user.id,
        username=payload.username,
        role=payload.role,
    )
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content=member.to_dict(),
    )


@router.get("/workspace/{workspace_id}/members")
async def list_members(
        workspace_id: int,
        current_user: User = Depends(get_current_user),
        session: Session = Depends(get_db),
):
    """列出工作空间成员。"""
    # 校验成员身份
    assert_member(session, workspace_id, current_user.id)
    members = workspace_service.list_members(session, workspace_id)
    return {"members": members, "code": 200}


@router.put("/workspace/{workspace_id}/members/{user_id}")
async def update_member_role(
        workspace_id: int,
        user_id: int,
        payload: MemberRoleUpdateRequest,
        current_user: User = Depends(get_current_user),
        session: Session = Depends(get_db),
):
    """变更成员角色（仅 admin）。"""
    member = workspace_service.update_member_role(
        session,
        workspace_id=workspace_id,
        actor_id=current_user.id,
        target_user_id=user_id,
        new_role=payload.role,
    )
    return member.to_dict()


@router.delete("/workspace/{workspace_id}/members/{user_id}")
async def remove_member(
        workspace_id: int,
        user_id: int,
        current_user: User = Depends(get_current_user),
        session: Session = Depends(get_db),
):
    """移除成员（仅 admin）。"""
    workspace_service.remove_member(
        session,
        workspace_id=workspace_id,
        actor_id=current_user.id,
        target_user_id=user_id,
    )
    return JSONResponse(status_code=200, content={"message": "已移除", "code": 200})
