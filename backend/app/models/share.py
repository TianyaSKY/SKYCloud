from app.infra.utc_datetime import UTCDateTime
import uuid

import datetime

from sqlalchemy import Integer, String, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from typing import TYPE_CHECKING

from app.infra.extensions import Base
from app.infra.datetime_utils import utc_now, local_isoformat, parse_legacy_datetime

if TYPE_CHECKING:
    from app.models.file import File
    from app.models.user import User


class Share(Base):
    """文件外链分享表：token 访问链接，可选过期时间。"""

    __tablename__ = "shares"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token: Mapped[str] = mapped_column(
        String(512), unique=True, nullable=False, default=lambda: str(uuid.uuid4())
    )
    file_id: Mapped[int] = mapped_column(Integer, ForeignKey("files.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[datetime.datetime | None] = mapped_column(UTCDateTime(), default=utc_now)
    expires_at: Mapped[datetime.datetime | None] = mapped_column(UTCDateTime(), nullable=True)  # 为空表示不过期

    file: Mapped["File"] = relationship("File", back_populates="shares")
    user: Mapped["User"] = relationship("User")

    def to_dict(self):
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
    def from_cache(cls, d: dict) -> "Share":
        return cls(
            id=d.get("id"),
            token=d.get("token"),
            file_id=d.get("file_id"),
            created_at=parse_legacy_datetime(d.get("created_at")),
            expires_at=parse_legacy_datetime(d.get("expires_at")),
        )
