import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, Integer, String, DateTime, ForeignKey
from sqlalchemy.orm import relationship

from app.extensions import Base


class Share(Base):
    __tablename__ = "shares"

    id = Column(Integer, primary_key=True)
    token = Column(
        String(512), unique=True, nullable=False, default=lambda: str(uuid.uuid4())
    )
    file_id = Column(Integer, ForeignKey("files.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    expires_at = Column(DateTime, nullable=True)  # 可选：过期时间



    file = relationship("File", back_populates="shares")
    user = relationship("User")

    def to_dict(self):
        return {
            "id": self.id,
            "token": self.token,
            "file_id": self.file_id,
            "file_name": self.file.name if self.file else None,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
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
