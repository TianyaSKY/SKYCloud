import uuid
from datetime import datetime

from sqlalchemy import Column, Integer, String, DateTime, ForeignKey
from sqlalchemy.orm import relationship

from app.extensions import Base
from app.infra.datetime_utils import beijing_now, local_isoformat


class Share(Base):
    """文件外链分享表：token 访问链接，可选过期时间。"""

    __tablename__ = "shares"

    id = Column(Integer, primary_key=True)
    token = Column(
        String(512), unique=True, nullable=False, default=lambda: str(uuid.uuid4())
    )
    file_id = Column(Integer, ForeignKey("files.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=beijing_now)
    expires_at = Column(DateTime, nullable=True)  # 为空表示不过期

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
            created_at=datetime.fromisoformat(d.get("created_at"))
            if d.get("created_at")
            else None,
            expires_at=datetime.fromisoformat(d.get("expires_at"))
            if d.get("expires_at")
            else None,
        )
