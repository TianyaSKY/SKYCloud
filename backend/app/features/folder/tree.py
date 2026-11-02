"""REST/MCP 用例与整理工具共享的目录不变量校验."""

from sqlalchemy.orm import Session

from app.exceptions import (
    BusinessRuleError,
    PermissionDeniedError,
    ResourceNotFoundError,
)
from app.models.folder import Folder

from . import repository

MAX_FOLDER_NAME_LENGTH = 128


def validate_name(name: object) -> None:
    """拒绝超出 1-128 字符持久化长度范围的名称."""
    if (
        not isinstance(name, str)
        or not name
        or len(name) > MAX_FOLDER_NAME_LENGTH
    ):
        message = "Folder name must contain 1 to 128 characters"
        raise BusinessRuleError(message)


def require_root(session: Session, workspace_id: int) -> int:
    """返回目录树操作所需的工作空间根文件夹 ID."""
    root = repository.root_id(session, workspace_id)
    if root is None:
        message = "Workspace root folder not found"
        raise ResourceNotFoundError(message)
    return root


def assert_mutable(session: Session, folder: Folder) -> None:
    """禁止修改或删除工作空间根文件夹本身."""
    if folder.id == require_root(session, folder.workspace_id):
        message = "Workspace root folder cannot be modified or deleted"
        raise BusinessRuleError(message)


def validate_parent(
    session: Session,
    workspace_id: int,
    parent_id: int | None,
    *,
    moving_id: int | None = None,
) -> int:
    """解析并校验目标父文件夹 ID."""
    parent_id = require_root(session, workspace_id) if parent_id is None else parent_id
    current = repository.get_folder(session, parent_id)
    seen: set[int] = set()
    while True:
        if current is None:
            message = "Parent folder not found"
            raise ResourceNotFoundError(message)
        if current.workspace_id != workspace_id:
            message = "Parent folder belongs to another workspace"
            raise PermissionDeniedError(message)
        if current.id == moving_id:
            message = "Cannot move a folder into itself or its descendants"
            raise BusinessRuleError(message)
        if current.id in seen:
            message = "Invalid folder hierarchy"
            raise BusinessRuleError(message)
        seen.add(current.id)
        if current.parent_id is None:
            break
        current = repository.get_folder(session, current.parent_id)
        if current is None:
            message = "Parent folder not found"
            raise ResourceNotFoundError(message)
    return parent_id
