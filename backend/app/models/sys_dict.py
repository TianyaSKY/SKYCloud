from datetime import datetime
from typing import cast

from sqlalchemy import Boolean, Column, DateTime, Integer, String

from app.extensions import Base
from app.infra.datetime_utils import beijing_now, local_isoformat


class SysDict(Base):
    """系统字典/配置键值表：运行时开关与可配置项。"""

    __tablename__ = "sys_dict"

    id = Column(Integer, primary_key=True)
    key = Column(String(100), unique=True, nullable=False)
    value = Column(String(255), nullable=False)
    des = Column(String(2048))  # 配置说明
    enable = Column(Boolean, default=True)
    created_at = Column(DateTime, default=beijing_now)

    def to_dict(self):
        return {
            "id": self.id,
            "key": self.key,
            "value": self.value,
            "enable": self.enable,
            "des": self.des,
            "created_at": local_isoformat(cast(datetime | None, self.created_at)),
        }

    @classmethod
    def from_cache(cls, d: dict) -> "SysDict":
        return cls(
            id=d.get("id"),
            key=d.get("key"),
            value=d.get("value"),
            enable=d.get("enable"),
            des=d.get("des"),
            created_at=datetime.fromisoformat(cast(str, d.get("created_at")))
            if cast(str | None, d.get("created_at"))
            else None,
        )
