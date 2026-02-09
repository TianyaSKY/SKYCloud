import datetime
import json

from fastapi import HTTPException
from fastapi_cache.decorator import cache

from app.extensions import db, redis_client
from app.models.user import User
from app.services import folder_service


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


def _user_key_builder(
    func, namespace: str = "", request=None, response=None, *args, **kwargs
):
    id_val = kwargs.get("id") or args[0]
    return f"user:profile:{id_val}"


@cache(expire=3600, key_builder=_user_key_builder)
async def _get_user_data(id: int) -> dict:
    user = User.query.get(id)
    if not user:
        return None
    return user.to_dict()


async def get_user(id: int) -> User:
    user_data = await _get_user_data(id)
    if not user_data:
        raise HTTPException(status_code=404, detail="User not found")
    return User.from_cache(user_data)


def update_user(id, data):
    user = User.query.get(id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.username = data.get("username", user.username)
    user.avatar = data.get("avatar", user.avatar)
    if "password" in data:
        user.set_password(data["password"])
    db.session.commit()

    cacher_key = f"user:profile:{id}"
    redis_client.delete(cacher_key)

    return user


def delete_user(id):
    user = User.query.get(id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    db.session.delete(user)
    db.session.commit()

    cacher_key = f"user:profile:{id}"
    redis_client.delete(cacher_key)
