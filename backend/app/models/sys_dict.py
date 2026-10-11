from app.infra.utc_datetime import UTCDateTime
from datetime import datetime
from typing import cast

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.extensions import Base
from app.infra.datetime_utils import utc_now, local_isoformat, parse_legacy_datetime


class SysDict(Base):
    """系统字典/配置键值表：运行时开关与可配置项。"""

    __tablename__ = "sys_dict"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    value: Mapped[str] = mapped_column(String(255), nullable=False)
    des: Mapped[str | None] = mapped_column(String(2048))  # 配置说明
    enable: Mapped[bool | None] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=utc_now)

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
            created_at=parse_legacy_datetime(cast(str | None, d.get("created_at"))),
        )
