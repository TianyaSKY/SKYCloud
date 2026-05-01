from fastapi import HTTPException

from app.cache import cacheable, evict_cache
from app.extensions import db
from app.models.user import User
from app.services import folder_service

USER_CACHE_PREFIX = "user:profile"
USER_CACHE_EXPIRE = 3600


def create_user(data):
    new_user = User(username=data["username"], role="common", avatar=data.get("avatar"))
    if "password" in data:
        new_user.set_password(data["password"])
    elif "password_hash" in data:
        new_user.password_hash = data["password_hash"]

    db.session.add(new_user)
    db.session.commit()
    # 同时也需要给新用户创建一个根目录
    folder_service.create_folder({"user_id": new_user.id, "name": "/"})
    return new_user


@cacheable(prefix=USER_CACHE_PREFIX, expire=USER_CACHE_EXPIRE)
def _get_user_data(id: int) -> dict | None:
    """缓存层：返回 dict 或 None（None 不会被缓存）"""
    user = db.session.get(User, id)
    return user.to_dict() if user else None


async def get_user(id: int) -> User:
    user_data = _get_user_data(id)
    if not user_data:
        raise HTTPException(status_code=404, detail="User not found")
    return User.from_cache(user_data)


def update_user(id, data):
    user = db.session.get(User, id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.username = data.get("username", user.username)
    user.avatar = data.get("avatar", user.avatar)
    if "password" in data:
        user.set_password(data["password"])
    db.session.commit()

    evict_cache(USER_CACHE_PREFIX, id)
    return user


def delete_user(id):
    user = db.session.get(User, id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    db.session.delete(user)
    db.session.commit()

    evict_cache(USER_CACHE_PREFIX, id)
