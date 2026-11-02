"""工作空间权限校验: 角色枚举与权限断言函数.

角色层级: admin > editor > viewer
- admin: 管理成员、删除空间、所有文件操作
- editor: 上传/编辑/移动/删除文件
- viewer: 只读 (列表、下载、预览).
"""

from enum import Enum

from sqlalchemy.orm import Session

from app.exceptions import PermissionDeniedError
from app.features.workspace.access_repository import lock_write_identity
from app.models.workspace import WorkspaceMember


class WorkspaceRole(str, Enum):
    """工作空间成员角色."""

    admin = "admin"
    editor = "editor"
    viewer = "viewer"


# 角色权限级别映射, 数值越大权限越高
ROLE_LEVEL: dict[str, int] = {
    "viewer": 1,
    "editor": 2,
    "admin": 3,
}


def get_member_role(
    session: Session,
    workspace_id: int,
    user_id: int,
) -> WorkspaceRole | None:
    """查询用户在指定工作空间中的角色; 非成员返回 None."""
    # 直接读取 role 列可观测到最新 SQL 状态, 即使当前 Session
    # 的 identity map 中残留了旧的 WorkspaceMember 实例。
    role = (
        session.query(WorkspaceMember.role)
        .filter_by(
            workspace_id=workspace_id,
            user_id=user_id,
        )
        .scalar()
    )
    return WorkspaceRole(role) if role is not None else None


def assert_member(session: Session, workspace_id: int, user_id: int) -> WorkspaceRole:
    """断言用户为工作空间成员, 返回其角色; 非成员抛 403."""
    role = get_member_role(session, workspace_id, user_id)
    if role is None:
        message = "您不是该工作空间的成员"
        raise PermissionDeniedError(message)
    return role


def assert_can_read(session: Session, workspace_id: int, user_id: int) -> WorkspaceRole:
    """断言用户有读取权限 (任何成员均可读取)."""
    return assert_member(session, workspace_id, user_id)


def assert_can_write(session: Session, workspace_id: int, user_id: int) -> None:
    """断言用户有写入权限 (editor 或 admin)."""
    role = assert_member(session, workspace_id, user_id)
    if ROLE_LEVEL.get(role.value, 0) < ROLE_LEVEL["editor"]:
        message = "需要编辑者或管理员权限"
        raise PermissionDeniedError(message)


def assert_admin(session: Session, workspace_id: int, user_id: int) -> None:
    """断言用户为工作空间管理员."""
    role = assert_member(session, workspace_id, user_id)
    if role != WorkspaceRole.admin:
        message = "需要管理员权限"
        raise PermissionDeniedError(message)


def check_role_level(role: str | WorkspaceRole, min_role: str) -> bool:
    """检查角色是否达到最低要求."""
    role_value = role.value if isinstance(role, WorkspaceRole) else role
    return ROLE_LEVEL.get(role_value, 0) >= ROLE_LEVEL.get(min_role, 0)


def assert_member_locked(
    session: Session,
    workspace_id: int,
    user_id: int,
) -> WorkspaceRole:
    """在调用方的发布事务内保持当前成员资格锁定."""
    member = lock_write_identity(session, workspace_id, user_id)
    if member is None:
        message = "您不是该工作空间的成员"
        raise PermissionDeniedError(message)
    return WorkspaceRole(member.role)


def assert_can_write_locked(session: Session, workspace_id: int, user_id: int) -> None:
    """在调用方的变更事务内保持编辑授权锁定."""
    role = assert_member_locked(session, workspace_id, user_id)
    if role not in (WorkspaceRole.editor, WorkspaceRole.admin):
        message = "需要编辑者或管理员权限"
        raise PermissionDeniedError(message)
