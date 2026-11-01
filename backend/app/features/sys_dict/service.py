"""系统字典:键值配置的同步读写与模型配置键防护."""

from typing import TypedDict

from sqlalchemy.orm import Session

from app.exceptions import BusinessRuleError, ResourceNotFoundError
from app.infra.llm.config import is_model_config_sys_dict_key
from app.models.sys_dict import SysDict, SysDictDict

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


def create_sys_dict(session: Session, data: SysDictCreateData) -> SysDict:
    """Create one dictionary entry unless it names model configuration."""
    _ensure_non_model_config_key(data.get("key"))
    new_dict = SysDict(
        key=data["key"], value=data["value"], des=data["des"], enable=data["enable"],
    )
    session.add(new_dict)
    session.commit()
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
    return sys_dict


def delete_sys_dict(session: Session, dict_id: int) -> None:
    """Delete one dictionary entry."""
    sys_dict = session.get(SysDict, dict_id)
    if not sys_dict:
        raise ResourceNotFoundError(_NOT_FOUND)
    session.delete(sys_dict)
    session.commit()


def get_sys_dict_all_sync(session: Session) -> list[SysDictDict]:
    """Return every non model-configuration entry straight from SQL."""
    sys_dicts = session.query(SysDict).all()
    return [
        sys_dict.to_dict()
        for sys_dict in sys_dicts
        if not is_model_config_sys_dict_key(sys_dict.key)
    ]


def get_sys_dict_sync(session: Session, dict_id: int) -> SysDictDict:
    """Return one dictionary entry by id synchronously."""
    for item in get_sys_dict_all_sync(session):
        if item["id"] == int(dict_id):
            return item
    raise ResourceNotFoundError(_NOT_FOUND)
