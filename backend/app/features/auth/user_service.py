"""用户资料 CRUD、缓存，以及改密/访问权限校验。"""

from sqlalchemy.orm import Session

from app.features.auth import account_repository
from app.infra.owned_work import run_owned

from app.exceptions import BusinessRuleError, PermissionDeniedError, ResourceNotFoundError
from app.infra.cache import cacheable, evict_cache
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember
from app.models.folder import Folder

USER_CACHE_PREFIX = "user:profile"
USER_CACHE_EXPIRE = 3600


def create_user(session: Session, data):
    new_user = User(username=data["username"], role="common", avatar=data.get("avatar"))
    if "password" in data:
        new_user.set_password(data["password"])
    elif "password_hash" in data:
        new_user.password_hash = data["password_hash"]

    session.add(new_user)
    session.flush()  # 获取 new_user.id

    # 创建私人工作空间 + owner 加为 admin 成员 + 根文件夹
    workspace = Workspace(name=f"{new_user.username}的空间", owner_id=new_user.id)
    session.add(workspace)
    session.flush()  # 获取 workspace.id

    member = WorkspaceMember(
        workspace_id=workspace.id,
        user_id=new_user.id,
        role="admin",
    )
    session.add(member)

    root_folder = Folder(name="/", workspace_id=workspace.id, parent_id=None)
    session.add(root_folder)

    session.commit()
    return new_user


@cacheable(
    prefix=USER_CACHE_PREFIX,
    expire=USER_CACHE_EXPIRE,
    key=lambda session, id, **_: id,
)
def _get_user_data(session: Session, id: int) -> dict | None:
    """缓存友好：返回 dict；None 表示不存在（不会被缓存）。"""
    user = session.get(User, id)
    return user.to_dict() if user else None


async def get_user(session: Session | None, id: int, *, session_factory=None,
                   actor_id: int | None = None) -> User:
    if session_factory is not None:
        def work():
            with session_factory() as owned:
                if actor_id is not None:
                    actor_role = account_repository.current_role(owned, actor_id)
                    if actor_role is None:
                        raise PermissionDeniedError("Permission denied")
                    ensure_user_access(actor_id, actor_role, id)
                role = account_repository.current_role(owned, id)
                user_data = _get_user_data(owned, id)
                if role is None or not user_data:
                    raise ResourceNotFoundError("User not found")
                return User.from_cache({**user_data, "role": role})
        return await run_owned(work)
    # Explicit synchronous-session callers retain their existing service seam.
    user_data = await run_owned(_get_user_data, session, id)
    if not user_data:
        raise ResourceNotFoundError("User not found")
    return User.from_cache(user_data)


def update_user(session: Session, id, data):
    if set(data) - {"username", "avatar"}:
        raise BusinessRuleError("Only username and avatar can be updated")
    user = session.get(User, id)
    if not user:
        raise ResourceNotFoundError("User not found")
    user.username = data.get("username", user.username)
    user.avatar = data.get("avatar", user.avatar)
    session.commit()

    evict_cache(USER_CACHE_PREFIX, id)
    return user


def delete_user(session: Session, id):
    # Lock the account and owned workspaces before enumerating resources.
    # FK writers cannot add a new owned space/runtime during deletion.
    user = account_repository.lock_user(session, id)
    if not user:
        raise ResourceNotFoundError("User not found")
    from app.features.workspace import docker_service

    owned = account_repository.lock_owned_workspaces(session, id)
    owned_ids = {workspace.id for workspace in owned}
    for workspace in owned:
        docker_service.remove_container(workspace, session)
    for runtime in account_repository.user_runtimes(session, id):
        if runtime.workspace_id not in owned_ids:
            docker_service.remove_runtime(runtime, session)
    # ORM backrefs would NULL non-null ownership/membership columns. The
    # database owns the complete cascade, including file cleanup triggers.
    account_repository.delete_account(session, id)
    session.commit()
    session.expire_all()

    evict_cache(USER_CACHE_PREFIX, id)


def change_password(
    session: Session,
    actor_id: int,
    actor_role: str,
    user_id: int,
    old_password: str,
    new_password: str,
) -> None:
    """本人或 admin 可改密；非 admin 必须校验旧密码。"""
    if actor_id != user_id and actor_role != "admin":
        raise PermissionDeniedError("Permission denied")
    user = session.query(User).filter_by(id=user_id).with_for_update().populate_existing().one_or_none()
    if not user:
        raise ResourceNotFoundError("User not found")
    if actor_role != "admin" and not user.check_password(old_password):
        raise BusinessRuleError("Old password is incorrect")
    user.set_password(new_password)
    user.auth_version += 1
    from app.mcp.token_service import _revoke_all_active
    from app.models.mcp_runtime_token import McpRuntimeToken
    from app.infra.datetime_utils import utc_now
    _revoke_all_active(session, user_id)
    session.query(McpRuntimeToken).filter_by(user_id=user_id).filter(McpRuntimeToken.revoked_at.is_(None)).update(
        {McpRuntimeToken.revoked_at: utc_now()}, synchronize_session=False
    )
    session.commit()
    evict_cache(USER_CACHE_PREFIX, user_id)


def ensure_user_access(actor_id: int, actor_role: str, user_id: int) -> None:
    """仅允许操作本人资料，或 admin 代操作。"""
    if actor_id != user_id and actor_role != "admin":
        raise PermissionDeniedError("Permission denied")


def authorize_account_mutation(session: Session, actor_id: int, user_id: int | None = None) -> str:
    """Keep current actor authorization locked through the caller's mutation commit."""
    identities = {actor_id} if user_id is None else {actor_id, user_id}
    users = {user.id: user for user in account_repository.lock_users(session, identities)}
    actor = users.get(actor_id)
    if actor is None or (user_id is None and actor.role != "admin"):
        raise PermissionDeniedError("Permission denied")
    if user_id is not None:
        ensure_user_access(actor_id, actor.role, user_id)
    return actor.role
