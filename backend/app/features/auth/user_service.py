"""用户资料 CRUD、缓存,以及改密/访问权限校验."""

from dataclasses import dataclass
from typing import Required, TypedDict, cast, overload

from sqlalchemy.orm import Session

from app.exceptions import (
    BusinessRuleError,
    PermissionDeniedError,
    ResourceNotFoundError,
)
from app.features.auth import account_repository
from app.features.workspace import docker_service
from app.infra.cache import (
    CacheableParams,
    cacheable,
    sql_cache_allowed,
    sql_cache_revision,
)
from app.infra.datetime_utils import utc_now
from app.infra.extensions import SessionFactory
from app.infra.owned_work import run_owned
from app.mcp import token_service as mcp_token_service
from app.models.folder import Folder
from app.models.mcp_runtime_token import McpRuntimeToken
from app.models.user import User, UserDict
from app.models.workspace import Workspace, WorkspaceMember

USER_CACHE_PREFIX = "user:profile"
USER_CACHE_EXPIRE = 3600

_NOT_FOUND = "User not found"
_DENIED = "Permission denied"


class CreateUserData(TypedDict, total=False):
    """创建账户时接受的入参字段结构."""

    username: Required[str]
    password: str
    password_hash: str
    avatar: str | None


class UpdateUserData(TypedDict, total=False):
    """部分更新允许修改的账户资料字段结构."""

    username: str
    avatar: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ChangePasswordParams:
    """修改密码请求参数以及执行操作的操作者身份."""

    actor_id: int
    actor_role: str
    user_id: int
    old_password: str
    new_password: str


def create_user(session: Session, data: CreateUserData) -> User:
    """创建新账户及其私有工作区和根文件夹."""
    new_user = User(
        username=data["username"], role="common", avatar=data.get("avatar"),
    )
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


def _user_cache_key(session: Session, user_id: int) -> str:
    """根据当前 SQL 缓存版本构建用户画像缓存键."""
    revision = sql_cache_revision(session, f"user:profile:{user_id}")
    return f"{user_id}:sql:{revision}"


def _user_cache_allowed(session: Session, user_id: int) -> bool:
    """对缓存画像函数使用共享的 SQL 缓存门禁函数."""
    return sql_cache_allowed(session, user_id)


@cacheable(
    CacheableParams(prefix=USER_CACHE_PREFIX, expire=USER_CACHE_EXPIRE),
    cache_allowed=_user_cache_allowed,
    key=_user_cache_key,
)
def get_user_data(session: Session, user_id: int) -> UserDict | None:
    """缓存友好:返回 dict;None 表示不存在(不会被缓存)."""
    user = session.get(User, user_id)
    return user.to_dict() if user else None


@overload
async def get_user(
    session: Session,
    user_id: int,
    *,
    session_factory: SessionFactory | None = None,
    actor_id: int | None = None,
) -> User: ...


@overload
async def get_user(
    session: None,
    user_id: int,
    *,
    session_factory: SessionFactory,
    actor_id: int | None = None,
) -> User: ...


async def get_user(
    session: Session | None,
    user_id: int,
    *,
    session_factory: SessionFactory | None = None,
    actor_id: int | None = None,
) -> User:
    """在 worker 线程加载账户画像, 提供操作者时执行访问权限校验."""
    if session_factory is not None:

        def work() -> User:
            with session_factory() as owned:
                if actor_id is not None:
                    actor_role = account_repository.current_role(owned, actor_id)
                    if actor_role is None:
                        raise PermissionDeniedError(_DENIED)
                    ensure_user_access(actor_id, actor_role, user_id)
                role = account_repository.current_role(owned, user_id)
                user_data = get_user_data(owned, user_id)
                if role is None or not user_data:
                    raise ResourceNotFoundError(_NOT_FOUND)
                profile: UserDict = {**user_data, "role": role}
                return User.from_cache(profile)

        return await run_owned(work)
    # 显式同步会话调用方保留其现有服务接口边界。
    # 未提供 Session 工厂时, 重载要求显式传入 Session。
    user_data = await run_owned(get_user_data, cast("Session", session), user_id)
    if not user_data:
        raise ResourceNotFoundError(_NOT_FOUND)
    return User.from_cache(user_data)


def update_user(session: Session, user_id: int, data: UpdateUserData) -> User:
    """更新单个账户的可变个人资料字段."""
    if set(data) - {"username", "avatar"}:
        message = "Only username and avatar can be updated"
        raise BusinessRuleError(message)
    user = session.get(User, user_id)
    if not user:
        raise ResourceNotFoundError(_NOT_FOUND)
    user.username = data.get("username", user.username)
    user.avatar = data.get("avatar", user.avatar)
    session.commit()

    return user


def delete_user(session: Session, user_id: int) -> None:
    """在解除其拥有的所有关联资源后删除该账户."""
    # 在枚举关联资源之前先锁定账户和所拥有的工作区。
    # 外键写入方无法在删除过程中新增工作区或运行时。
    user = account_repository.lock_user(session, user_id)
    if not user:
        raise ResourceNotFoundError(_NOT_FOUND)

    owned = account_repository.lock_owned_workspaces(session, user_id)
    owned_ids = {workspace.id for workspace in owned}
    for workspace in owned:
        docker_service.remove_container(workspace, session)
    for runtime in account_repository.user_runtimes(session, user_id):
        if runtime.workspace_id not in owned_ids:
            docker_service.remove_runtime(runtime, session)
    # ORM 反向引用会将非空列置空。数据库
    # 自身管理完整的级联删除, 包括文件清理触发器。
    account_repository.delete_account(session, user_id)
    session.commit()
    session.expire_all()


def change_password(session: Session, params: ChangePasswordParams) -> None:
    """本人或 admin 可改密;非 admin 必须校验旧密码."""
    if params.actor_id != params.user_id and params.actor_role != "admin":
        raise PermissionDeniedError(_DENIED)
    user = (
        session.query(User)
        .filter_by(id=params.user_id)
        .with_for_update()
        .populate_existing()
        .one_or_none()
    )
    if not user:
        raise ResourceNotFoundError(_NOT_FOUND)
    if params.actor_role != "admin" and not user.check_password(
        params.old_password,
    ):
        message = "Old password is incorrect"
        raise BusinessRuleError(message)
    user.set_password(params.new_password)
    user.auth_version += 1
    mcp_token_service.revoke_all_active(session, params.user_id)
    session.query(McpRuntimeToken).filter_by(user_id=params.user_id).filter(
        McpRuntimeToken.revoked_at.is_(None),
    ).update({McpRuntimeToken.revoked_at: utc_now()}, synchronize_session=False)
    session.commit()


def ensure_user_access(
    actor_id: int, actor_role: str | None, user_id: int,
) -> None:
    """仅允许操作本人资料,或 admin 代操作."""
    if actor_id != user_id and actor_role != "admin":
        raise PermissionDeniedError(_DENIED)


def authorize_account_mutation(
    session: Session, actor_id: int, user_id: int | None = None,
) -> str:
    """在调用方的变更提交生命周期中保持当前操作者鉴权锁定有效."""
    identities = {actor_id} if user_id is None else {actor_id, user_id}
    users = {
        user.id: user
        for user in account_repository.lock_users(session, identities)
    }
    actor = users.get(actor_id)
    if (
        actor is None
        or actor.role is None
        or (user_id is None and actor.role != "admin")
    ):
        raise PermissionDeniedError(_DENIED)
    if user_id is not None:
        ensure_user_access(actor_id, actor.role, user_id)
    return actor.role
