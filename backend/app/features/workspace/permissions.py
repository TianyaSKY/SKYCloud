"""工作空间权限校验：角色枚举与权限断言函数。

角色层级：admin > editor > viewer
- admin: 管理成员、删除空间、所有文件操作
- editor: 上传/编辑/移动/删除文件
- viewer: 只读（列表、下载、预览）
"""

from enum import Enum

from sqlalchemy.orm import Session

from app.exceptions import PermissionDeniedError, ResourceNotFoundError
from app.models.workspace import WorkspaceMember


class WorkspaceRole(str, Enum):
    """工作空间成员角色。"""
    admin = "admin"
    editor = "editor"
    viewer = "viewer"


# 角色权限级别映射，数值越大权限越高
ROLE_LEVEL: dict[str, int] = {
    "viewer": 1,
    "editor": 2,
    "admin": 3,
}


def get_member_role(session: Session, workspace_id: int, user_id: int) -> WorkspaceRole | None:
    """查询用户在指定工作空间中的角色；非成员返回 None。"""
    member = (
        session.query(WorkspaceMember)
        .filter_by(workspace_id=workspace_id, user_id=user_id)
        .first()
    )
    if not member:
        return None
    return WorkspaceRole(member.role)


def assert_member(session: Session, workspace_id: int, user_id: int) -> WorkspaceRole:
    """断言用户为工作空间成员，返回其角色；非成员抛 403。"""
    role = get_member_role(session, workspace_id, user_id)
    if role is None:
        raise PermissionDeniedError("您不是该工作空间的成员")
    return role


def assert_can_read(session: Session, workspace_id: int, user_id: int) -> WorkspaceRole:
    """断言用户有读取权限（任何成员均可读取）。"""
    return assert_member(session, workspace_id, user_id)


def assert_can_write(session: Session, workspace_id: int, user_id: int) -> None:
    """断言用户有写入权限（editor 或 admin）。"""
    role = assert_member(session, workspace_id, user_id)
    if ROLE_LEVEL.get(role.value, 0) < ROLE_LEVEL["editor"]:
        raise PermissionDeniedError("需要编辑者或管理员权限")


def assert_admin(session: Session, workspace_id: int, user_id: int) -> None:
    """断言用户为工作空间管理员。"""
    role = assert_member(session, workspace_id, user_id)
    if role != WorkspaceRole.admin:
        raise PermissionDeniedError("需要管理员权限")


def check_role_level(role: str | WorkspaceRole, min_role: str) -> bool:
    """检查角色是否达到最低要求。"""
    role_value = role.value if isinstance(role, WorkspaceRole) else role
    return ROLE_LEVEL.get(role_value, 0) >= ROLE_LEVEL.get(min_role, 0)
