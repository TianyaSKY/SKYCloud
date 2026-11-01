"""工作空间路由: 空间 CRUD 与成员管理."""

from typing import Annotated

import jwt
from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.orm import Session

from app.api.contracts import SuccessEnvelope, SuccessResponse, success_payload
from app.api.dependencies import get_current_user
from app.features.workspace import docker_service, ui_tickets
from app.features.workspace import service as workspace_service
from app.features.workspace.permissions import assert_can_write, assert_member
from app.features.workspace.schemas import (
    MemberInviteRequest,
    MemberListPayload,
    MemberListResponse,
    MemberResponse,
    MemberRoleUpdateRequest,
    MessagePayload,
    RuntimeWorkspaceResponse,
    UiTicketResponse,
    WorkspaceCreateRequest,
    WorkspaceDetailResponse,
    WorkspaceDetailRuntimeData,
    WorkspaceListPayload,
    WorkspaceListResponse,
    WorkspaceMessageResponse,
    WorkspaceResponse,
    WorkspaceRuntimeData,
    WorkspaceUpdateRequest,
)
from app.features.workspace.ui_tickets import UiTicket
from app.infra.extensions import SECRET_KEY, SessionFactory, get_session_factory
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceDict, WorkspaceMemberDict

router = APIRouter(tags=["workspace"])

CurrentUser = Annotated[User, Depends(get_current_user)]
SessionFactoryDep = Annotated[SessionFactory, Depends(get_session_factory)]
DELETED_MESSAGE = "已删除"


# ---------------------------------------------------------------------------
# 工作空间 CRUD
# ---------------------------------------------------------------------------


@router.post(
    "/workspace",
    response_model=SuccessResponse[WorkspaceResponse],
    status_code=status.HTTP_201_CREATED,
)
def create_workspace(
    payload: WorkspaceCreateRequest,
    current_user: CurrentUser,
    session_factory: SessionFactoryDep,
) -> SuccessEnvelope[WorkspaceDict]:
    """创建新的工作空间."""
    with session_factory() as session:
        workspace = workspace_service.create_workspace(
            session,
            owner_id=current_user.id,
            name=payload.name,
            description=payload.description,
        )
        return success_payload(workspace.to_dict())


@router.get("/workspace", response_model=SuccessResponse[WorkspaceListResponse])
def list_workspaces(
    current_user: CurrentUser,
    session_factory: SessionFactoryDep,
) -> SuccessEnvelope[WorkspaceListPayload]:
    """列出当前用户加入的所有工作空间."""
    with session_factory() as session:
        rows = workspace_service.get_user_workspace_rows(session, current_user.id)
        workspaces = docker_service.list_summaries(session, rows, current_user.id)
        return success_payload({"workspaces": workspaces})


def _docker_response(
    session: Session,
    workspace: Workspace,
    user_id: int,
) -> WorkspaceRuntimeData:
    summary = docker_service.summary(session, workspace, user_id)
    return {**workspace.to_dict(), **summary}


@router.get(
    "/workspace/{workspace_id}",
    response_model=SuccessResponse[WorkspaceDetailResponse],
)
def get_workspace(
    workspace_id: int,
    current_user: CurrentUser,
    session_factory: SessionFactoryDep,
) -> SuccessEnvelope[WorkspaceDetailRuntimeData]:
    """获取工作空间详情 (含成员列表)."""
    with session_factory() as session:
        # 校验成员身份
        assert_member(session, workspace_id, current_user.id)
        detail = workspace_service.get_workspace_detail(
            session,
            workspace_id,
            current_user.id,
        )
        summary = docker_service.summary(
            session,
            workspace_service.get_workspace(session, workspace_id),
            current_user.id,
        )
        merged: WorkspaceDetailRuntimeData = {**detail, **summary}
        return success_payload(merged)


# ---------------------------------------------------------------------------
# OpenCode Docker 工作区
# ---------------------------------------------------------------------------


@router.post(
    "/workspace/{workspace_id}/opencode/ui-ticket",
    response_model=SuccessResponse[UiTicketResponse],
)
def issue_ui_ticket(
    workspace_id: int,
    request: Request,
    response: Response,
    current_user: CurrentUser,
    session_factory: SessionFactoryDep,
) -> SuccessEnvelope[UiTicket]:
    """Issue a single-use UI launch credential for the caller's running runtime."""
    with session_factory() as session:
        # get_current_user has already checked the exact Bearer signature/scope.
        claims = jwt.decode(
            request.headers["authorization"].split(" ", 1)[1],
            SECRET_KEY,
            algorithms=["HS256"],
            options={"verify_exp": False},
        )
        response.headers["Cache-Control"] = "no-store"
        return success_payload(
            ui_tickets.issue(
                session,
                current_user.id,
                workspace_id,
                parent_session=claims.get("sid"),
            ),
        )




# ---------------------------------------------------------------------------
# Per-user OpenCode Runtime API
# ---------------------------------------------------------------------------


@router.get(
    "/workspace/{workspace_id}/opencode/runtime",
    response_model=SuccessResponse[RuntimeWorkspaceResponse],
)
def get_opencode_runtime(
    workspace_id: int,
    current_user: CurrentUser,
    session_factory: SessionFactoryDep,
) -> SuccessEnvelope[WorkspaceRuntimeData]:
    """Return only the current member's OpenCode Runtime status."""
    with session_factory() as session:
        assert_member(session, workspace_id, current_user.id)
        workspace = workspace_service.get_workspace(session, workspace_id)
        return success_payload(_docker_response(session, workspace, current_user.id))


@router.post(
    "/workspace/{workspace_id}/opencode/runtime/start",
    response_model=SuccessResponse[RuntimeWorkspaceResponse],
)
def start_opencode_runtime(
    workspace_id: int,
    current_user: CurrentUser,
    session_factory: SessionFactoryDep,
) -> SuccessEnvelope[WorkspaceRuntimeData]:
    """Start the caller's runtime, provisioning it when absent."""
    with session_factory() as session:
        assert_can_write(session, workspace_id, current_user.id)
        workspace = workspace_service.get_workspace(session, workspace_id)
        docker_service.start(session, workspace, current_user.id)
        return success_payload(_docker_response(session, workspace, current_user.id))


@router.post(
    "/workspace/{workspace_id}/opencode/runtime/stop",
    response_model=SuccessResponse[RuntimeWorkspaceResponse],
)
def stop_opencode_runtime(
    workspace_id: int,
    current_user: CurrentUser,
    session_factory: SessionFactoryDep,
) -> SuccessEnvelope[WorkspaceRuntimeData]:
    """Stop the caller's runtime and revoke its credentials."""
    with session_factory() as session:
        assert_can_write(session, workspace_id, current_user.id)
        workspace = workspace_service.get_workspace(session, workspace_id)
        docker_service.stop(session, workspace, current_user.id)
        return success_payload(_docker_response(session, workspace, current_user.id))


@router.post(
    "/workspace/{workspace_id}/opencode/runtime/restart",
    response_model=SuccessResponse[RuntimeWorkspaceResponse],
)
def restart_opencode_runtime(
    workspace_id: int,
    current_user: CurrentUser,
    session_factory: SessionFactoryDep,
) -> SuccessEnvelope[WorkspaceRuntimeData]:
    """Restart the caller's runtime and refresh its MCP credentials."""
    with session_factory() as session:
        assert_can_write(session, workspace_id, current_user.id)
        workspace = workspace_service.get_workspace(session, workspace_id)
        docker_service.restart(session, workspace, current_user.id)
        return success_payload(_docker_response(session, workspace, current_user.id))


@router.post(
    "/workspace/{workspace_id}/opencode/runtime/setup-mcp",
    response_model=SuccessResponse[RuntimeWorkspaceResponse],
)
def setup_opencode_runtime_mcp(
    workspace_id: int,
    current_user: CurrentUser,
    session_factory: SessionFactoryDep,
) -> SuccessEnvelope[WorkspaceRuntimeData]:
    """Refresh the caller's MCP token and Runtime configuration."""
    with session_factory() as session:
        assert_can_write(session, workspace_id, current_user.id)
        workspace = workspace_service.get_workspace(session, workspace_id)
        docker_service.setup_mcp(session, workspace, current_user.id)
        return success_payload(_docker_response(session, workspace, current_user.id))


@router.put(
    "/workspace/{workspace_id}",
    response_model=SuccessResponse[WorkspaceResponse],
)
def update_workspace(
    workspace_id: int,
    payload: WorkspaceUpdateRequest,
    current_user: CurrentUser,
    session_factory: SessionFactoryDep,
) -> SuccessEnvelope[WorkspaceDict]:
    """更新工作空间信息 (仅 admin)."""
    with session_factory() as session:
        workspace = workspace_service.update_workspace(
            session,
            workspace_id=workspace_id,
            user_id=current_user.id,
            name=payload.name,
            description=payload.description,
        )
        return success_payload(workspace.to_dict())


@router.delete(
    "/workspace/{workspace_id}",
    response_model=SuccessResponse[WorkspaceMessageResponse],
)
def delete_workspace(
    workspace_id: int,
    current_user: CurrentUser,
    session_factory: SessionFactoryDep,
) -> SuccessEnvelope[MessagePayload]:
    """删除工作空间 (仅 owner)."""
    with session_factory() as session:
        workspace_service.delete_workspace(session, workspace_id, current_user.id)
        return success_payload({"message": DELETED_MESSAGE})


# ---------------------------------------------------------------------------
# 成员管理
# ---------------------------------------------------------------------------


@router.post(
    "/workspace/{workspace_id}/members",
    response_model=SuccessResponse[MemberResponse],
    status_code=status.HTTP_201_CREATED,
)
def invite_member(
    workspace_id: int,
    payload: MemberInviteRequest,
    current_user: CurrentUser,
    session_factory: SessionFactoryDep,
) -> SuccessEnvelope[WorkspaceMemberDict]:
    """邀请成员加入工作空间 (仅 admin)."""
    with session_factory() as session:
        member = workspace_service.invite_member(
            session,
            workspace_id=workspace_id,
            actor_id=current_user.id,
            username=payload.username,
            role=payload.role,
        )
        return success_payload(member.to_dict())


@router.get(
    "/workspace/{workspace_id}/members",
    response_model=SuccessResponse[MemberListResponse],
)
def list_members(
    workspace_id: int,
    current_user: CurrentUser,
    session_factory: SessionFactoryDep,
) -> SuccessEnvelope[MemberListPayload]:
    """列出工作空间成员."""
    with session_factory() as session:
        # 校验成员身份
        assert_member(session, workspace_id, current_user.id)
        members = workspace_service.list_members(session, workspace_id)
        return success_payload({"members": members})


@router.put(
    "/workspace/{workspace_id}/members/{user_id}",
    response_model=SuccessResponse[MemberResponse],
)
def update_member_role(
    workspace_id: int,
    user_id: int,
    payload: MemberRoleUpdateRequest,
    current_user: CurrentUser,
    session_factory: SessionFactoryDep,
) -> SuccessEnvelope[WorkspaceMemberDict]:
    """变更成员角色 (仅 admin)."""
    with session_factory() as session:
        member = workspace_service.update_member_role(
            session,
            workspace_id=workspace_id,
            actor_id=current_user.id,
            target_user_id=user_id,
            new_role=payload.role,
        )
        return success_payload(member.to_dict())


@router.delete(
    "/workspace/{workspace_id}/members/{user_id}",
    response_model=SuccessResponse[WorkspaceMessageResponse],
)
def remove_member(
    workspace_id: int,
    user_id: int,
    current_user: CurrentUser,
    session_factory: SessionFactoryDep,
) -> SuccessEnvelope[MessagePayload]:
    """移除成员 (仅 admin)."""
    with session_factory() as session:
        workspace_service.remove_member(
            session,
            workspace_id=workspace_id,
            actor_id=current_user.id,
            target_user_id=user_id,
        )
        return success_payload({"message": "已移除"})
