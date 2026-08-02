"""工作空间业务逻辑：空间 CRUD、成员管理、通知。"""

from loguru import logger
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.exceptions import BusinessRuleError, PermissionDeniedError, ResourceNotFoundError
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember
from app.models.folder import Folder
from app.models.inbox import Inbox
from app.features.workspace.permissions import WorkspaceRole, assert_admin, assert_can_write


def create_workspace(session: Session, owner_id: int, name: str, description: str | None = None) -> Workspace:
    """创建工作空间 + 根文件夹 + owner 加为 admin 成员。"""
    workspace = Workspace(name=name, description=description, owner_id=owner_id)
    session.add(workspace)
    session.flush()  # 获取 workspace.id

    # owner 自动成为 admin 成员
    member = WorkspaceMember(
        workspace_id=workspace.id,
        user_id=owner_id,
        role=WorkspaceRole.admin.value,
    )
    session.add(member)

    # 创建根文件夹
    root_folder = Folder(name="/", workspace_id=workspace.id, parent_id=None)
    session.add(root_folder)

    session.commit()
    logger.info("创建工作空间：ws_id={}, name={}, owner={}", workspace.id, name, owner_id)
    return workspace


def get_user_workspaces(session: Session, user_id: int) -> list[dict]:
    """列出用户拥有的和加入的工作空间。"""
    workspaces = (
        session.query(Workspace)
        .join(WorkspaceMember, Workspace.id == WorkspaceMember.workspace_id)
        .filter(WorkspaceMember.user_id == user_id)
        .order_by(Workspace.created_at.desc())
        .all()
    )
    result = []
    for ws in workspaces:
        ws_dict = ws.to_dict()
        # 查询用户在该空间的角色
        member = (
            session.query(WorkspaceMember)
            .filter_by(workspace_id=ws.id, user_id=user_id)
            .first()
        )
        ws_dict["my_role"] = member.role if member else None
        ws_dict["member_count"] = (
            session.query(WorkspaceMember)
            .filter_by(workspace_id=ws.id)
            .count()
        )
        result.append(ws_dict)
    return result


def get_workspace(session: Session, workspace_id: int) -> Workspace:
    """获取工作空间，不存在则 404。"""
    workspace = session.get(Workspace, workspace_id)
    if not workspace:
        raise ResourceNotFoundError("工作空间不存在")
    return workspace


def get_workspace_detail(session: Session, workspace_id: int, user_id: int) -> dict:
    """获取工作空间详情（含成员列表）。"""
    workspace = get_workspace(session, workspace_id)
    ws_dict = workspace.to_dict()

    # 查询当前用户角色
    my_member = (
        session.query(WorkspaceMember)
        .filter_by(workspace_id=workspace_id, user_id=user_id)
        .first()
    )
    ws_dict["my_role"] = my_member.role if my_member else None

    # 成员列表
    members = (
        session.query(WorkspaceMember, User)
        .join(User, WorkspaceMember.user_id == User.id)
        .filter(WorkspaceMember.workspace_id == workspace_id)
        .order_by(WorkspaceMember.joined_at.asc())
        .all()
    )
    ws_dict["members"] = [
        {
            **member.to_dict(),
            "username": user.username,
            "avatar": user.avatar,
        }
        for member, user in members
    ]
    return ws_dict


def update_workspace(session: Session, workspace_id: int, user_id: int, name: str | None, description: str | None) -> Workspace:
    """更新工作空间信息（仅 admin）。"""
    assert_admin(session, workspace_id, user_id)
    workspace = get_workspace(session, workspace_id)

    if name is not None:
        workspace.name = name
    if description is not None:
        workspace.description = description

    session.commit()
    logger.info("更新工作空间：ws_id={}, user={}", workspace_id, user_id)
    return workspace


def delete_workspace(session: Session, workspace_id: int, user_id: int) -> None:
    """删除工作空间（仅 owner）。"""
    workspace = get_workspace(session, workspace_id)
    if workspace.owner_id != user_id:
        raise PermissionDeniedError("只有空间所有者可以删除空间")

    # 容器不受数据库外键约束，删除协作空间前需显式释放 Docker 资源。
    from app.features.workspace import docker_service
    docker_service.remove_container(workspace, session)
    session.delete(workspace)
    session.commit()
    logger.info("删除工作空间：ws_id={}, user={}", workspace_id, user_id)


def invite_member(session: Session, workspace_id: int, actor_id: int, username: str, role: str) -> WorkspaceMember:
    """邀请成员加入工作空间（仅 admin）。"""
    assert_admin(session, workspace_id, actor_id)

    # 查找被邀请用户
    target_user = session.query(User).filter_by(username=username).first()
    if not target_user:
        raise ResourceNotFoundError(f"用户 '{username}' 不存在")

    # 检查是否已是成员
    existing = (
        session.query(WorkspaceMember)
        .filter_by(workspace_id=workspace_id, user_id=target_user.id)
        .first()
    )
    if existing:
        raise BusinessRuleError("该用户已是空间成员")

    member = WorkspaceMember(
        workspace_id=workspace_id,
        user_id=target_user.id,
        role=role,
        invited_by=actor_id,
    )
    session.add(member)

    # 发送通知
    workspace = get_workspace(session, workspace_id)
    notification = Inbox(
        user_id=target_user.id,
        title="工作空间邀请",
        content=f"您已被邀请加入工作空间「{workspace.name}」，角色：{role}",
        type="workspace",
    )
    session.add(notification)

    session.commit()
    logger.info("邀请成员：ws_id={}, user={}, role={}", workspace_id, target_user.id, role)
    return member


def remove_member(session: Session, workspace_id: int, actor_id: int, target_user_id: int) -> None:
    """移除成员（仅 admin；不可移除 owner）。"""
    assert_admin(session, workspace_id, actor_id)

    workspace = get_workspace(session, workspace_id)
    if workspace.owner_id == target_user_id:
        raise BusinessRuleError("不可移除空间所有者")

    member = (
        session.query(WorkspaceMember)
        .filter_by(workspace_id=workspace_id, user_id=target_user_id)
        .with_for_update()
        .first()
    )
    if not member:
        raise ResourceNotFoundError("该用户不是空间成员")

    # A removed member's OpenCode credential must stop working immediately and
    # the corresponding container must not survive the membership deletion.
    from app.features.workspace.runtime_service import get_runtime
    from app.features.workspace import docker_service

    runtime = get_runtime(session, workspace_id, target_user_id)
    if runtime:
        docker_service.remove_runtime(runtime, session)
        # Runtime records are owned by the workspace membership.  Once that
        # membership is gone, keep no dormant container identity or session
        # binding that could be accidentally reused later.
        session.delete(runtime)

    session.delete(member)

    # 发送通知
    notification = Inbox(
        user_id=target_user_id,
        title="工作空间移除",
        content=f"您已被移出工作空间「{workspace.name}」",
        type="workspace",
    )
    session.add(notification)

    session.commit()
    logger.info("移除成员：ws_id={}, user={}", workspace_id, target_user_id)


def update_member_role(session: Session, workspace_id: int, actor_id: int, target_user_id: int, new_role: str) -> WorkspaceMember:
    """变更成员角色（仅 admin；不可变更 owner）。"""
    assert_admin(session, workspace_id, actor_id)

    workspace = get_workspace(session, workspace_id)
    if workspace.owner_id == target_user_id:
        raise BusinessRuleError("不可变更空间所有者的角色")

    member = (
        session.query(WorkspaceMember)
        .filter_by(workspace_id=workspace_id, user_id=target_user_id)
        .with_for_update()
        .first()
    )
    if not member:
        raise ResourceNotFoundError("该用户不是空间成员")

    old_role = member.role
    member.role = new_role
    session.commit()
    # Revoke the old role-bound runtime credential before provisioning a new
    # one.  A running OpenCode runtime is refreshed in place so a role change
    # takes effect without requiring the user to restart the container.  If
    # Docker/configuration is unavailable, the token remains revoked and the
    # runtime can be repaired through the explicit setup/restart endpoint.
    from app.mcp import runtime_token_service
    from app.features.workspace.runtime_service import get_runtime
    runtime = get_runtime(session, workspace_id, target_user_id)
    if runtime:
        runtime_token_service.revoke_runtime_tokens(session, int(runtime.id))
        session.commit()
        if runtime.status == "running" and runtime.container_id:
            try:
                from app.features.workspace import docker_service

                docker_service.setup_mcp(
                    session,
                    workspace,
                    target_user_id,
                    runtime=runtime,
                )
            except Exception:
                logger.exception(
                    "角色变更后刷新 OpenCode MCP 配置失败：ws_id={}, user={}, runtime_id={}",
                    workspace_id,
                    target_user_id,
                    runtime.id,
                )
    logger.info("变更成员角色：ws_id={}, user={}, {} -> {}", workspace_id, target_user_id, old_role, new_role)
    return member


def list_members(session: Session, workspace_id: int) -> list[dict]:
    """列出工作空间成员。"""
    members = (
        session.query(WorkspaceMember, User)
        .join(User, WorkspaceMember.user_id == User.id)
        .filter(WorkspaceMember.workspace_id == workspace_id)
        .order_by(WorkspaceMember.joined_at.asc())
        .all()
    )
    return [
        {
            **member.to_dict(),
            "username": user.username,
            "avatar": user.avatar,
        }
        for member, user in members
    ]
