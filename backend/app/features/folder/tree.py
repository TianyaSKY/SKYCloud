"""Directory invariants shared by REST/MCP use cases and organizer tools."""
from app.exceptions import BusinessRuleError, PermissionDeniedError, ResourceNotFoundError
from . import repository


def validate_name(name):
    if not isinstance(name, str) or not name or len(name) > 128:
        raise BusinessRuleError('Folder name must contain 1 to 128 characters')


def require_root(session, workspace_id):
    root = repository.root_id(session, workspace_id)
    if root is None:
        raise ResourceNotFoundError('Workspace root folder not found')
    return root


def assert_mutable(session, folder):
    if folder.id == require_root(session, folder.workspace_id):
        raise BusinessRuleError('Workspace root folder cannot be modified or deleted')


def validate_parent(session, workspace_id, parent_id, *, moving_id=None):
    parent_id = require_root(session, workspace_id) if parent_id is None else parent_id
    current = repository.get_folder(session, parent_id)
    if current is None:
        raise ResourceNotFoundError('Parent folder not found')
    seen = set()
    while current is not None:
        if current.workspace_id != workspace_id:
            raise PermissionDeniedError('Parent folder belongs to another workspace')
        if current.id == moving_id:
            raise BusinessRuleError('Cannot move a folder into itself or its descendants')
        if current.id in seen:
            raise BusinessRuleError('Invalid folder hierarchy')
        seen.add(current.id)
        if current.parent_id is None:
            break
        current = repository.get_folder(session, current.parent_id)
        if current is None:
            raise ResourceNotFoundError('Parent folder not found')
    return parent_id
