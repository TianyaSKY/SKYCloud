"""支持可选过期时间的文件外链分享模型."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, TypedDict

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infra.datetime_utils import (
    local_isoformat,
    parse_legacy_datetime,
    utc_now,
)
from app.infra.extensions import Base
from app.infra.utc_datetime import UTCDateTime

if TYPE_CHECKING:
    from app.models.file import File
    from app.models.user import User


class ShareDict(TypedDict):
    """文件分享记录的公开表示模型."""

    id: int
    token: str
    file_id: int
    file_name: str | None
    created_at: str | None
    expires_at: str | None
    link: str


class Share(Base):
    """外部文件分享: 包含唯一口令链接与可选有效期的模型."""

    __tablename__ = "shares"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token: Mapped[str] = mapped_column(
        String(512),
        unique=True,
        nullable=False,
        default=lambda: str(uuid.uuid4()),
    )
    file_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("files.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        default=utc_now,
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        nullable=True,
    )

    file: Mapped["File"] = relationship("File", back_populates="shares")
    user: Mapped["User"] = relationship("User")

    def to_dict(self) -> ShareDict:
        """返回文件分享的公开字典表示."""
        return {
            "id": self.id,
            "token": self.token,
            "file_id": self.file_id,
            "file_name": self.file.name if self.file else None,
            "created_at": local_isoformat(self.created_at),
            "expires_at": local_isoformat(self.expires_at),
            "link": f"/api/share/{self.token}",
        }

    @classmethod
    def from_cache(cls, d: ShareDict) -> "Share":
        """根据缓存字段构建游离态分享记录."""
        return cls(
            id=d.get("id"),
            token=d.get("token"),
            file_id=d.get("file_id"),
            created_at=parse_legacy_datetime(d.get("created_at")),
            expires_at=parse_legacy_datetime(d.get("expires_at")),
        )
