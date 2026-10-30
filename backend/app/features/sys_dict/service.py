"""系统字典:键值配置的读写、缓存与模型配置键防护."""

from typing import TypedDict

from sqlalchemy.orm import Session

from app.exceptions import BusinessRuleError, ResourceNotFoundError
from app.infra.cache import (
    CacheableParams,
    cacheable,
    evict_cache,
    sql_cache_allowed,
    sql_cache_revision,
)
from app.infra.llm.config import is_model_config_sys_dict_key
from app.infra.owned_work import run_owned
from app.models.sys_dict import SysDict, SysDictDict

SYS_DICT_CACHE_PREFIX = "sys_dict:all"
SYS_DICT_CACHE_EXPIRE = 3600
MODEL_CONFIG_DB_ERROR = (
    "Model configuration must be managed by environment variables, "
    "not stored in sys_dict."
)
_NOT_FOUND = "SysDict not found"


class SysDictCreateData(TypedDict):
    """Fields required to create one dictionary entry."""

    key: str
    value: str
    des: str | None
    enable: bool


class SysDictUpdateData(TypedDict, total=False):
    """Fields a partial dictionary update may change."""

    key: str
    value: str
    des: str | None
    enable: bool


def _ensure_non_model_config_key(key: str | None) -> None:
    if is_model_config_sys_dict_key(key):
        raise BusinessRuleError(MODEL_CONFIG_DB_ERROR)


def _invalidate_sys_dict_cache() -> None:
    evict_cache(SYS_DICT_CACHE_PREFIX, "all")


def create_sys_dict(session: Session, data: SysDictCreateData) -> SysDict:
    """Create one dictionary entry unless it names model configuration."""
    _ensure_non_model_config_key(data.get("key"))
    new_dict = SysDict(
        key=data["key"], value=data["value"], des=data["des"], enable=data["enable"],
    )
    session.add(new_dict)
    session.commit()
    _invalidate_sys_dict_cache()
    return new_dict


def update_sys_dict(
    session: Session, dict_id: int, data: SysDictUpdateData,
) -> SysDict:
    """Update one dictionary entry, guarding model configuration keys."""
    sys_dict = session.get(SysDict, dict_id)
    if not sys_dict:
        raise ResourceNotFoundError(_NOT_FOUND)
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


def delete_sys_dict(session: Session, dict_id: int) -> None:
    """Delete one dictionary entry."""
    sys_dict = session.get(SysDict, dict_id)
    if not sys_dict:
        raise ResourceNotFoundError(_NOT_FOUND)
    session.delete(sys_dict)
    session.commit()
    _invalidate_sys_dict_cache()


def _sys_dict_cache_key(session: Session) -> str:
    """Build the dictionary cache key from the current SQL cache revision."""
    revision = sql_cache_revision(session, SYS_DICT_CACHE_PREFIX)
    return f"all:sql:{revision}"


def _sys_dict_cache_allowed(session: Session) -> bool:
    """Use the shared SQL cache gate with the cached dictionary signature."""
    return sql_cache_allowed(session)


@cacheable(
    CacheableParams(prefix=SYS_DICT_CACHE_PREFIX, expire=SYS_DICT_CACHE_EXPIRE),
    key=_sys_dict_cache_key,
    cache_allowed=_sys_dict_cache_allowed,
)
def _get_sys_dict_all_cached(session: Session) -> list[SysDictDict]:
    """Return every non model-configuration entry through the cache."""
    sys_dicts = session.query(SysDict).all()
    return [
        sys_dict.to_dict()
        for sys_dict in sys_dicts
        if not is_model_config_sys_dict_key(sys_dict.key)
    ]


def get_sys_dict_all_sync(session: Session) -> list[SysDictDict]:
    """Read the cache/SQL on the caller's owning worker."""
    return _get_sys_dict_all_cached(session)


async def get_sys_dict_all(session: Session) -> list[SysDictDict]:
    """Return every non model-configuration entry."""
    return await run_owned(_get_sys_dict_all_cached, session)


async def get_sys_dict_by_key(session: Session, key: str) -> SysDict | None:
    """Return the newest enabled entry for one key, if any."""
    if is_model_config_sys_dict_key(key):
        return None
    all_dicts = await get_sys_dict_all(session)
    for item in all_dicts:
        if item["key"] == key and item["enable"]:
            return SysDict.from_cache(item)
    return None


def get_sys_dict_by_key_sync(session: Session, key: str) -> SysDict | None:
    """Look up the newest enabled entry for one key synchronously."""
    if is_model_config_sys_dict_key(key):
        return None
    return (
        session.query(SysDict)
        .filter_by(key=key, enable=True)
        .order_by(SysDict.id.desc())
        .first()
    )


async def get_sys_dict(session: Session, dict_id: int) -> SysDictDict:
    """Return one dictionary entry by id."""
    all_dicts = await get_sys_dict_all(session)
    for item in all_dicts:
        if item["id"] == int(dict_id):
            return item
    raise ResourceNotFoundError(_NOT_FOUND)


def get_sys_dict_sync(session: Session, dict_id: int) -> SysDictDict:
    """Return one dictionary entry by id synchronously."""
    for item in get_sys_dict_all_sync(session):
        if item["id"] == int(dict_id):
            return item
    raise ResourceNotFoundError(_NOT_FOUND)
