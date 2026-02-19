from fastapi import HTTPException
from fastapi_cache.decorator import cache

from app.extensions import db, redis_client
from app.models.sys_dict import SysDict
from app.services.model_config import is_model_config_sys_dict_key

CACHE_KEY = "sys_dict:all"
CACHE_EXPIRE = 3600
MODEL_CONFIG_DB_ERROR = (
    "Model configuration must be managed by environment variables, "
    "not stored in sys_dict."
)


def _ensure_non_model_config_key(key: str | None) -> None:
    if is_model_config_sys_dict_key(key):
        raise HTTPException(status_code=400, detail=MODEL_CONFIG_DB_ERROR)


def create_sys_dict(data):
    _ensure_non_model_config_key(data.get("key"))
    new_dict = SysDict(
        key=data["key"], value=data["value"], des=data["des"], enable=data["enable"]
    )
    db.session.add(new_dict)
    db.session.commit()
    redis_client.delete(CACHE_KEY)
    return new_dict


def update_sys_dict(id, data):
    sys_dict = SysDict.query.get(id)
    if not sys_dict:
        raise HTTPException(status_code=404, detail="SysDict not found")
    next_key = data.get("key", sys_dict.key)
    _ensure_non_model_config_key(next_key)
    if is_model_config_sys_dict_key(sys_dict.key):
        raise HTTPException(status_code=400, detail=MODEL_CONFIG_DB_ERROR)
    sys_dict.key = data.get("key", sys_dict.key)
    sys_dict.value = data.get("value", sys_dict.value)
    sys_dict.des = data.get("des", sys_dict.des)
    sys_dict.enable = data.get("enable", sys_dict.enable)
    db.session.commit()
    redis_client.delete(CACHE_KEY)
    return sys_dict


def delete_sys_dict(id):
    sys_dict = SysDict.query.get(id)
    if not sys_dict:
        raise HTTPException(status_code=404, detail="SysDict not found")
    db.session.delete(sys_dict)
    db.session.commit()
    redis_client.delete(CACHE_KEY)


@cache(key_builder=lambda *args, **kwargs: CACHE_KEY, expire=CACHE_EXPIRE)
async def get_sys_dict_all():
    sys_dicts = SysDict.query.all()
    data = [
        sys_dict.to_dict()
        for sys_dict in sys_dicts
        if not is_model_config_sys_dict_key(sys_dict.key)
    ]
    return data


async def get_sys_dict_by_key(key):
    if is_model_config_sys_dict_key(key):
        return None
    all_dicts = await get_sys_dict_all()
    for item in all_dicts:
        if item["key"] == key and item["enable"]:
            return SysDict.from_cache(item)
    return None


def get_sys_dict_by_key_sync(key):
    """Synchronous lookup for worker/thread contexts."""
    if is_model_config_sys_dict_key(key):
        return None
    return (
        SysDict.query.filter_by(key=key, enable=True)
        .order_by(SysDict.id.desc())
        .first()
    )


async def get_sys_dict(id):
    all_dicts = await get_sys_dict_all()
    for item in all_dicts:
        if item["id"] == int(id):
            return item
    raise HTTPException(status_code=404, detail="SysDict not found")
