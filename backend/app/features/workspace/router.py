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
    return detail


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
