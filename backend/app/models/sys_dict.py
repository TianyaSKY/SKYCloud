"""System dictionary rows holding runtime switches and settings."""

from datetime import datetime
from typing import TypedDict

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.datetime_utils import (
    local_isoformat,
    parse_legacy_datetime,
    utc_now,
)
from app.infra.extensions import Base
from app.infra.utc_datetime import UTCDateTime


class SysDictDict(TypedDict):
    """Public representation of a system dictionary row."""

    id: int
    key: str
    value: str
    enable: bool | None
    des: str | None
    created_at: str | None


class SysDict(Base):
    """System dictionary key/value row for runtime configuration."""

    __tablename__ = "sys_dict"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    value: Mapped[str] = mapped_column(String(255), nullable=False)
    des: Mapped[str | None] = mapped_column(String(2048))
    enable: Mapped[bool | None] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        default=utc_now,
    )

    def to_dict(self) -> SysDictDict:
        """Return the public dictionary entry representation."""
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
        """Build a detached dictionary row from cached fields."""
        return cls(
            id=d.get("id"),
            key=d.get("key"),
            value=d.get("value"),
            enable=d.get("enable"),
            des=d.get("des"),
            created_at=parse_legacy_datetime(d.get("created_at")),
        )
