"""工作空间业务逻辑: 空间 CRUD、成员管理、通知, 以及 Runtime 生命周期的迁移入口."""

from loguru import logger
from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from app.exceptions import (
    BusinessRuleError,
    PermissionDeniedError,
    ResourceNotFoundError,
)
from app.features.workspace import docker_service, lifecycle_repository, runtime_service
from app.features.workspace.permissions import (
    WorkspaceRole,
    assert_admin,
)
from app.features.workspace.schemas import (
    MemberDetail,
    WorkspaceDetailData,
    WorkspaceListEntry,
)
from app.mcp import runtime_token_service
from app.models.folder import Folder
from app.models.inbox import Inbox
from app.models.opencode_runtime import OpenCodeRuntime
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember


def create_workspace(
    session: Session,
    owner_id: int,
    name: str,
    description: str | None = None,
) -> Workspace:
    """创建工作空间 + 根文件夹 + owner 加为 admin 成员."""
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
    logger.info(
        "创建工作空间: ws_id={}, name={}, owner={}",
        workspace.id,
        name,
        owner_id,
    )
    return workspace


def _workspace_list_entry(
    workspace: Workspace, role: str, member_count: int,
) -> WorkspaceListEntry:
    """将工作空间快照与其成员身份概要组合."""
    return {
        **workspace.to_dict(),
        "my_role": role,
        "member_count": member_count,
    }


def get_user_workspace_rows(
    session: Session,
    user_id: int,
) -> list[tuple[WorkspaceListEntry, OpenCodeRuntime | None]]:
    """在单条 SQL 中批量查询成员数量及仅限调用方的运行时状态."""
    counts = (
        session.query(
            WorkspaceMember.workspace_id,
            func.count(WorkspaceMember.id).label("member_count"),
        )
        .group_by(WorkspaceMember.workspace_id)
        .subquery()
    )
    rows = (
        session.query(Workspace, WorkspaceMember.role, counts.c.member_count,
                      OpenCodeRuntime)
        .join(WorkspaceMember, Workspace.id == WorkspaceMember.workspace_id)
        .join(counts, Workspace.id == counts.c.workspace_id)
        .outerjoin(
            OpenCodeRuntime,
            and_(
                OpenCodeRuntime.workspace_id == Workspace.id,
                OpenCodeRuntime.user_id == user_id,
            ),
        )
        .filter(WorkspaceMember.user_id == user_id)
        .order_by(Workspace.created_at.desc())
        .all()
    )
    return [
        (
            _workspace_list_entry(workspace, role, member_count),
            runtime,
        )
        for workspace, role, member_count, runtime in rows
    ]


def get_user_workspaces(session: Session, user_id: int) -> list[WorkspaceListEntry]:
    """列出用户拥有的和加入的工作空间."""
    return [workspace for workspace, _ in get_user_workspace_rows(session, user_id)]


def get_workspace(session: Session, workspace_id: int) -> Workspace:
    """获取工作空间, 不存在则 404."""
    workspace = session.get(Workspace, workspace_id)
    if not workspace:
        message = "工作空间不存在"
        raise ResourceNotFoundError(message)
    return workspace


def _member_detail(member: WorkspaceMember, user: User) -> MemberDetail:
    """将成员关系快照与用户的公开展示字段组合."""
    return {**member.to_dict(), "username": user.username, "avatar": user.avatar}


def _member_details(session: Session, workspace_id: int) -> list[MemberDetail]:
    members = (
        session.query(WorkspaceMember, User)
        .join(User, WorkspaceMember.user_id == User.id)
        .filter(WorkspaceMember.workspace_id == workspace_id)
        .order_by(WorkspaceMember.joined_at.asc())
        .all()
    )
    return [
        _member_detail(member, user)
        for member, user in members
    ]


def get_workspace_detail(
    session: Session,
    workspace_id: int,
    user_id: int,
) -> WorkspaceDetailData:
    """获取工作空间详情 (含成员列表)."""
    workspace = get_workspace(session, workspace_id)

    # 查询当前用户角色
    my_member = (
        session.query(WorkspaceMember)
        .filter_by(workspace_id=workspace_id, user_id=user_id)
        .first()
    )
    return WorkspaceDetailData(
        **workspace.to_dict(),
        my_role=my_member.role if my_member else None,
        members=_member_details(session, workspace_id),
    )


def update_workspace(
    session: Session,
    workspace_id: int,
    user_id: int,
    name: str | None,
    description: str | None,
) -> Workspace:
    """更新工作空间信息 (仅 admin)."""
    assert_admin(session, workspace_id, user_id)
    workspace = get_workspace(session, workspace_id)

    if name is not None:
        workspace.name = name
    if description is not None:
        workspace.description = description

    session.commit()
    logger.info("更新工作空间: ws_id={}, user={}", workspace_id, user_id)
    return workspace


def delete_workspace(session: Session, workspace_id: int, user_id: int) -> None:
    """删除工作空间 (仅 owner)."""
    workspace = lifecycle_repository.lock_workspace(session, workspace_id)
    if workspace is None:
        message = "工作空间不存在"
        raise ResourceNotFoundError(message)
    if workspace.owner_id != user_id:
        message = "只有空间所有者可以删除空间"
        raise PermissionDeniedError(message)

    # 容器清理意图随数据库删除提交, 由 Worker 异步释放资源.
    docker_service.remove_container(workspace, session)
    lifecycle_repository.delete_workspace(session, workspace_id)
    session.commit()
    session.expire_all()
    logger.info("删除工作空间: ws_id={}, user={}", workspace_id, user_id)


def invite_member(
    session: Session,
    workspace_id: int,
    actor_id: int,
    username: str,
    role: str,
) -> WorkspaceMember:
    """邀请成员加入工作空间 (仅 admin)."""
    assert_admin(session, workspace_id, actor_id)

    # 查找被邀请用户
    target_user = session.query(User).filter_by(username=username).first()
    if not target_user:
        message = f"用户 '{username}' 不存在"
        raise ResourceNotFoundError(message)

    # 检查是否已是成员
    existing = (
        session.query(WorkspaceMember)
        .filter_by(workspace_id=workspace_id, user_id=target_user.id)
        .first()
    )
    if existing:
        message = "该用户已是空间成员"
        raise BusinessRuleError(message)

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
        content=f"您已被邀请加入工作空间「{workspace.name}」, 角色: {role}",
        type="workspace",
    )
    session.add(notification)

    session.commit()
    logger.info(
        "邀请成员: ws_id={}, user={}, role={}",
        workspace_id,
        target_user.id,
        role,
    )
    return member


def remove_member(
    session: Session,
    workspace_id: int,
    actor_id: int,
    target_user_id: int,
) -> None:
    """移除成员 (仅 admin; 不可移除 owner)."""
    assert_admin(session, workspace_id, actor_id)

    workspace = get_workspace(session, workspace_id)
    if workspace.owner_id == target_user_id:
        message = "不可移除空间所有者"
        raise BusinessRuleError(message)

    member = (
        session.query(WorkspaceMember)
        .filter_by(workspace_id=workspace_id, user_id=target_user_id)
        .with_for_update()
        .first()
    )
    if not member:
        message = "该用户不是空间成员"
        raise ResourceNotFoundError(message)

    # 被移除成员的 OpenCode 凭据必须立即失效,
    # 且其对应的容器在成员记录删除后绝不能继续存活。
    runtime = runtime_service.get_runtime(session, workspace_id, target_user_id)
    if runtime:
        docker_service.remove_runtime(runtime, session)
        # Runtime 记录归属于工作空间成员关系。一旦该成员关系
        # 被删除, 绝不残留任何休眠的容器身份或会话绑定,
        # 防止后续被意外错误复用。
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
    logger.info("移除成员: ws_id={}, user={}", workspace_id, target_user_id)


def update_member_role(
    session: Session,
    workspace_id: int,
    actor_id: int,
    target_user_id: int,
    new_role: str,
) -> WorkspaceMember:
    """变更成员角色 (仅 admin; 不可变更 owner)."""
    assert_admin(session, workspace_id, actor_id)

    workspace = get_workspace(session, workspace_id)
    if workspace.owner_id == target_user_id:
        message = "不可变更空间所有者的角色"
        raise BusinessRuleError(message)

    member = (
        session.query(WorkspaceMember)
        .filter_by(workspace_id=workspace_id, user_id=target_user_id)
        .with_for_update()
        .first()
    )
    if not member:
        message = "该用户不是空间成员"
        raise ResourceNotFoundError(message)

    old_role = member.role
    member.role = new_role
    session.commit()
    # 在编排新凭据前, 优先吊销绑定旧角色的运行时凭据。
    # 运行中的 OpenCode runtime 会就地刷新使其生效,
    # 无需用户手动重启容器。若 Docker 或配置不可用,
    # 令牌依然保持吊销状态, 该运行时
    # 可通过显式设置/重启接口进行修复。
    runtime = runtime_service.get_runtime(session, workspace_id, target_user_id)
    if runtime:
        runtime_token_service.revoke_runtime_tokens(session, int(runtime.id))
        session.commit()
        if runtime.status == "running" and runtime.container_id:
            try:
                docker_service.setup_mcp(
                    session,
                    workspace,
                    target_user_id,
                    runtime=runtime,
                )
            except Exception as exc:
                logger.error(
                    "角色变更后刷新 OpenCode MCP 配置失败: "
                    "ws_id={}, user={}, runtime_id={}, error_type={}",
                    workspace_id,
                    target_user_id,
                    runtime.id,
                    type(exc).__name__,
                )
    logger.info(
        "变更成员角色: ws_id={}, user={}, {} -> {}",
        workspace_id,
        target_user_id,
        old_role,
        new_role,
    )
    return member


def list_members(session: Session, workspace_id: int) -> list[MemberDetail]:
    """列出工作空间成员."""
    return _member_details(session, workspace_id)
