from app.infra.cache import cacheable, evict_cache
from app.exceptions import BusinessRuleError, ResourceNotFoundError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from app.models.sys_dict import SysDict
from app.infra.llm.config import is_model_config_sys_dict_key

SYS_DICT_CACHE_PREFIX = "sys_dict:all"
SYS_DICT_CACHE_EXPIRE = 3600
MODEL_CONFIG_DB_ERROR = (
    "Model configuration must be managed by environment variables, "
    "not stored in sys_dict."
)


def _ensure_non_model_config_key(key: str | None) -> None:
    if is_model_config_sys_dict_key(key):
        raise BusinessRuleError(MODEL_CONFIG_DB_ERROR)


def _invalidate_sys_dict_cache() -> None:
    evict_cache(SYS_DICT_CACHE_PREFIX, "all")


def create_sys_dict(session: Session, data):
    _ensure_non_model_config_key(data.get("key"))
    new_dict = SysDict(
        key=data["key"], value=data["value"], des=data["des"], enable=data["enable"]
    )
    session.add(new_dict)
    session.commit()
    _invalidate_sys_dict_cache()
    return new_dict


def update_sys_dict(session: Session, id, data):
    sys_dict = session.get(SysDict, id)
    if not sys_dict:
        raise ResourceNotFoundError("SysDict not found")
    next_key = data.get("key", sys_dict.key)
    _ensure_non_model_config_key(next_key)
    if is_model_config_sys_dict_key(sys_dict.key):
        raise BusinessRuleError(MODEL_CONFIG_DB_ERROR)
    sys_dict.key = data.get("key", sys_dict.key)
    sys_dict.value = data.get("value", sys_dict.value)
    sys_dict.des = data.get("des", sys_dict.des)
    sys_dict.enable = data.get("enable", sys_dict.enable)
    session.commit()
    _invalidate_sys_dict_cache()
    return sys_dict


def delete_sys_dict(session: Session, id):
    sys_dict = session.get(SysDict, id)
    if not sys_dict:
        raise ResourceNotFoundError("SysDict not found")
    session.delete(sys_dict)
    session.commit()
    _invalidate_sys_dict_cache()


@cacheable(prefix=SYS_DICT_CACHE_PREFIX, expire=SYS_DICT_CACHE_EXPIRE, key=lambda session: "all")
def _get_sys_dict_all_cached(session: Session) -> list[dict]:
    sys_dicts = session.query(SysDict).all()
    return [
        sys_dict.to_dict()
        for sys_dict in sys_dicts
        if not is_model_config_sys_dict_key(sys_dict.key)
    ]


def get_sys_dict_all_sync(session: Session):
    """Read the cache/SQL on the caller's owning worker."""
    return _get_sys_dict_all_cached(session)


async def get_sys_dict_all(session: Session):
    return await run_in_threadpool(_get_sys_dict_all_cached, session)


async def get_sys_dict_by_key(session: Session, key):
    if is_model_config_sys_dict_key(key):
        return None
    all_dicts = await get_sys_dict_all(session)
    for item in all_dicts:
        if item["key"] == key and item["enable"]:
            return SysDict.from_cache(item)
    return None


def get_sys_dict_by_key_sync(session: Session, key):
    """Synchronous lookup for worker/thread contexts."""
    if is_model_config_sys_dict_key(key):
        return None
    return (
        session.query(SysDict).filter_by(key=key, enable=True)
        .order_by(SysDict.id.desc())
        .first()
    )


async def get_sys_dict(session: Session, id):
    all_dicts = await get_sys_dict_all(session)
    for item in all_dicts:
        if item["id"] == int(id):
            return item
    raise ResourceNotFoundError("SysDict not found")


def get_sys_dict_sync(session: Session, id):
    for item in get_sys_dict_all_sync(session):
        if item["id"] == int(id):
            return item
    raise ResourceNotFoundError("SysDict not found")
