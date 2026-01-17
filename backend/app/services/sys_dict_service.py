import json

from flask import abort

from app.extensions import db, redis_client
from app.models.sys_dict import SysDict

CACHE_KEY = "sys_dict:all"
CACHE_EXPIRE = 3600


def create_sys_dict(data):
    new_dict = SysDict(
        key=data['key'],
        value=data['value'],
        des=data['des'],
        enable=data['enable']
    )
    db.session.add(new_dict)
    db.session.commit()
    redis_client.delete(CACHE_KEY)
    return new_dict


def update_sys_dict(id, data):
    sys_dict = SysDict.query.get_or_404(id)
    sys_dict.key = data.get('key', sys_dict.key)
    sys_dict.value = data.get('value', sys_dict.value)
    sys_dict.des = data.get('des', sys_dict.des)
    sys_dict.enable = data.get('enable', sys_dict.enable)
    db.session.commit()
    redis_client.delete(CACHE_KEY)
    return sys_dict


def delete_sys_dict(id):
    sys_dict = SysDict.query.get_or_404(id)
    db.session.delete(sys_dict)
    db.session.commit()
    redis_client.delete(CACHE_KEY)


def get_sys_dict_all():
    cached_data = redis_client.get(CACHE_KEY)
    if cached_data:
        return json.loads(cached_data)
    sys_dicts = SysDict.query.all()
    data = [sys_dict.to_dict() for sys_dict in sys_dicts]
    redis_client.set(CACHE_KEY, json.dumps(data), ex=CACHE_EXPIRE)
    return data


def get_sys_dict_by_key(key):
    all_dicts = get_sys_dict_all()
    for item in all_dicts:
        if item['key'] == key and item['enable']:
            return SysDict.from_cache(item)
    return None


def get_sys_dict(id):
    all_dicts = get_sys_dict_all()
    for item in all_dicts:
        if item['id'] == int(id):
            return item
    abort(404)
