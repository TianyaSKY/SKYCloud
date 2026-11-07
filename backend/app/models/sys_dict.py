"""保存运行时开关与全局配置的系统字典表模型."""

from datetime import datetime
from typing import TypedDict

from sqlalchemy import Boolean, Integer, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.datetime_utils import (
    local_isoformat,
    parse_legacy_datetime,
    utc_now,
)
from app.infra.extensions import Base
from app.infra.utc_datetime import UTCDateTime


class SysDictDict(TypedDict):
    """系统字典记录的公开表示模型."""

    id: int
    key: str
    value: str | None
    enable: bool | None
    des: str | None
    created_at: str | None


class SysDict(Base):
    """用于运行时动态配置的系统字典键值对模型."""

    __tablename__ = "sys_dict"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    value: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    des: Mapped[str | None] = mapped_column(String(255))
    enable: Mapped[bool | None] = mapped_column(
        Boolean,
        default=True,
        server_default=text("true"),
    )
    created_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        server_default=func.now(),
    )

    def to_dict(self) -> SysDictDict:
        """返回字典条目的公开字典表示."""
        return {
            "id": self.id,
            "key": self.key,
            "value": self.value,
            "enable": self.enable,
            "des": self.des,
            "created_at": local_isoformat(self.created_at),
        }

    @classmethod
    def from_cache(cls, d: SysDictDict) -> "SysDict":
        """根据缓存字段构建游离态字典记录."""
        return cls(
            id=d.get("id"),
            key=d.get("key"),
            value=d.get("value"),
            enable=d.get("enable"),
            des=d.get("des"),
            created_at=parse_legacy_datetime(d.get("created_at")),
        )
