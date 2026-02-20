import os
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import Column, Integer, String, BigInteger, DateTime, ForeignKey, Index
from sqlalchemy.orm import relationship

from app.extensions import Base, UPLOAD_FOLDER, _scoped_session


class File(Base):
    __tablename__ = "files"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    file_path = Column(String(512), nullable=False)  # 现在只保存文件名
    file_size = Column(BigInteger)  # 使用 BigInteger 存储字节数，方便计算容量
    mime_type = Column(String(255))  # 如 'image/jpeg', 'application/pdf'

    # AI 相关字段
    status = Column(String(20), default="pending")  # pending, processing, success, fail
    vector_info = Column(Vector(1024))
    description = Column(String(4096))  # 对于文件的描述

    uploader_id = Column(Integer, ForeignKey("users.id"))
    parent_id = Column(Integer, ForeignKey("folder.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.now)

    # 关系映射
    uploader = relationship("User", backref="files")

    # 级联删除：当文件被删除时，自动删除关联的分享记录
    shares = relationship("Share", back_populates="file", cascade="all, delete-orphan")

    # 添加 query 属性用于兼容 Flask-SQLAlchemy 风格的查询
    query = _scoped_session.query_property()

    __table_args__ = (
        Index(
            "file_vector_idx",
            "vector_info",
            postgresql_using="hnsw",
            postgresql_ops={"vector_info": "vector_l2_ops"},
        ),
    )

    def get_abs_path(self):
        """获取文件的绝对路径"""
        return os.path.join(UPLOAD_FOLDER, self.file_path)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "description": self.description,
            "file_size": self.file_size,
            "mime_type": self.mime_type,
            "uploader_id": self.uploader_id,
            "parent_id": self.parent_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    @classmethod
    def from_cache(cls, d: dict) -> "File":
        return cls(
            id=d.get("id"),
            name=d.get("name"),
            status=d.get("status"),
            description=d.get("description"),
            file_size=d.get("file_size"),
            mime_type=d.get("mime_type"),
            uploader_id=d.get("uploader_id"),
            parent_id=d.get("parent_id"),
            created_at=datetime.fromisoformat(d.get("created_at"))
            if d.get("created_at")
            else None,
        )
