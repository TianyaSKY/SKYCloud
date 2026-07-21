"""用户资料 CRUD、缓存，以及改密/访问权限校验。"""

from sqlalchemy.orm import Session

from app.exceptions import BusinessRuleError, PermissionDeniedError, ResourceNotFoundError
from app.infra.cache import cacheable, evict_cache
from app.models.user import User
from app.services import folder_service

USER_CACHE_PREFIX = "user:profile"
USER_CACHE_EXPIRE = 3600


def create_user(session: Session, data):
    new_user = User(username=data["username"], role="common", avatar=data.get("avatar"))
    if "password" in data:
        new_user.set_password(data["password"])
    elif "password_hash" in data:
        new_user.password_hash = data["password_hash"]

    session.add(new_user)
    session.commit()
    # 新用户必须有根目录，后续文件/文件夹均挂在其下
    folder_service.create_folder(session, {"user_id": new_user.id, "name": "/"})
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


async def get_user(session: Session, id: int) -> User:
    user_data = _get_user_data(session, id)
    if not user_data:
        raise ResourceNotFoundError("User not found")
    return User.from_cache(user_data)


def update_user(session: Session, id, data):
    user = session.get(User, id)
    if not user:
        raise ResourceNotFoundError("User not found")
    user.username = data.get("username", user.username)
    user.avatar = data.get("avatar", user.avatar)
    if "password" in data:
        user.set_password(data["password"])
    session.commit()

    evict_cache(USER_CACHE_PREFIX, id)
    return user


def delete_user(session: Session, id):
    user = session.get(User, id)
    if not user:
        raise ResourceNotFoundError("User not found")
    session.delete(user)
    session.commit()

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
    user = session.get(User, user_id)
    if not user:
        raise ResourceNotFoundError("User not found")
    if actor_role != "admin" and not user.check_password(old_password):
        raise BusinessRuleError("Old password is incorrect")
    update_user(session, user_id, {"password": new_password})


def ensure_user_access(actor_id: int, actor_role: str, user_id: int) -> None:
    """仅允许操作本人资料，或 admin 代操作。"""
    if actor_id != user_id and actor_role != "admin":
        raise PermissionDeniedError("Permission denied")
