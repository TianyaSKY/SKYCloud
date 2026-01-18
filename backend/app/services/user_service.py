import datetime
import json

from app.extensions import db, redis_client
from app.models.user import User
from app.services import folder_service


def create_user(data):
    new_user = User(
        username=data['username'],
        role="common",
        avatar=data.get('avatar')
    )
    if 'password' in data:
        new_user.set_password(data['password'])
    elif 'password_hash' in data:
        new_user.password_hash = data['password_hash']

    db.session.add(new_user)
    db.session.commit()
    # 同时也需要给新用户创建一个根目录
    folder_service.create_folder({"user_id": new_user.id, "name": "/"})
    return new_user


def get_user(id):
    cacher_key = f"user:profile:{id}"
    cached_user_json = redis_client.get(cacher_key)
    if cached_user_json:
        cached_user = json.loads(cached_user_json)
        cached_user["id"] = int(cached_user["id"])
        cached_user["created_at"] = datetime.datetime.fromisoformat(cached_user["created_at"])
        return from_dict2entity(cached_user)
    user = User.query.get_or_404(id)
    redis_client.set(cacher_key, json.dumps(user.to_dict()), ex=3600)
    return user


def update_user(id, data):
    user = User.query.get_or_404(id)
    user.username = data.get('username', user.username)
    user.avatar = data.get('avatar', user.avatar)
    if 'password' in data:
        user.set_password(data['password'])
    db.session.commit()

    cacher_key = f"user:profile:{id}"
    redis_client.set(cacher_key, json.dumps(user.to_dict()), ex=3600)

    return user


def delete_user(id):
    user = User.query.get_or_404(id)
    db.session.delete(user)
    db.session.commit()

    cacher_key = f"user:profile:{id}"
    redis_client.delete(cacher_key)


def from_dict2entity(d: dict) -> User:
    user = User()
    for k, v in d.items():
        setattr(user, k, v)
    return user
