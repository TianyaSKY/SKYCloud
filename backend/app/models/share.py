from app.infra.utc_datetime import UTCDateTime
import uuid

from sqlalchemy import Column, Integer, String, ForeignKey
from sqlalchemy.orm import relationship

from app.infra.extensions import Base
from app.infra.datetime_utils import utc_now, local_isoformat, parse_legacy_datetime


class Share(Base):
    """文件外链分享表：token 访问链接，可选过期时间。"""

    __tablename__ = "shares"

    id = Column(Integer, primary_key=True)
    token = Column(
        String(512), unique=True, nullable=False, default=lambda: str(uuid.uuid4())
    )
    file_id = Column(Integer, ForeignKey("files.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(UTCDateTime(), default=utc_now)
    expires_at = Column(UTCDateTime(), nullable=True)  # 为空表示不过期

    file = relationship("File", back_populates="shares")
    user = relationship("User")

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
