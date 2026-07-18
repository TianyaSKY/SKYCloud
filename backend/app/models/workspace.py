"""工作区模型：每用户绑定的 opencode 容器实例。"""

from sqlalchemy import Column, Integer, String, DateTime, Enum, Text

from app.extensions import Base
from app.infra.datetime_utils import beijing_now, local_isoformat


class Workspace(Base):
    """opencode 工作区表：记录 Docker 容器生命周期与鉴权密码。"""

    __tablename__ = "workspaces"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    name = Column(String(120), nullable=False)
    # Docker container ID（64 位 hex），docker run 成功后写入
    container_id = Column(String(64), default=None)
    # opencode Basic Auth 随机密码
    container_password = Column(String(64), nullable=False)
    # creating | running | stopped | error
    status = Column(
        Enum("creating", "running", "stopped", "error", name="workspace_status"),
        default="creating",
    )
    # status == error 时的可读错误信息
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
