"""Workspace model — tracks opencode container instances per user."""

from sqlalchemy import Column, Integer, String, DateTime, Enum, Text

from app.datetime_utils import beijing_now, local_isoformat
from app.extensions import Base


class Workspace(Base):
    __tablename__ = "workspaces"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    name = Column(String(120), nullable=False)
    # Docker container ID (64-char hex), set after `docker run`
    container_id = Column(String(64), default=None)
    # Random password used for opencode Basic Auth
    container_password = Column(String(64), nullable=False)
    # Status: creating | running | stopped | error
    status = Column(
        Enum("creating", "running", "stopped", "error", name="workspace_status"),
        default="creating",
    )
    # Human-readable error message when status == "error"
    error_message = Column(Text, default=None)
    created_at = Column(DateTime, default=beijing_now)
    updated_at = Column(DateTime, default=beijing_now, onupdate=beijing_now)

    def __repr__(self):
        return f"<Workspace {self.id} user={self.user_id} status={self.status}>"

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "name": self.name,
            "container_id": self.container_id[:12] if self.container_id else None,
            "status": self.status,
            "error_message": self.error_message,
            "created_at": local_isoformat(self.created_at),
            "updated_at": local_isoformat(self.updated_at),
        }
