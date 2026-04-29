import os
from datetime import datetime
from typing import cast

from pgvector.sqlalchemy import Vector
from sqlalchemy import Column, Integer, String, BigInteger, DateTime, ForeignKey, Index
from sqlalchemy.orm import relationship

from app.extensions import Base, UPLOAD_FOLDER


class File(Base):
    __tablename__ = "files"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    file_path = Column(String(512), nullable=False)  # 现在只保存文件名
    file_size = Column(BigInteger)  # 使用 BigInteger 存储字节数，方便计算容量
    mime_type = Column(String(255))  # 如 'image/jpeg', 'application/pdf'
    content_hash = Column(String(64))  # 文件内容 SHA-256，用于秒传去重

    # AI 相关字段
    # pending, processing, success, fail
    status = Column(String(20), default="pending")
    vector_info = Column(Vector(1024))
    description = Column(String(4096))  # 对于文件的描述

    uploader_id = Column(Integer, ForeignKey("users.id"))
    parent_id = Column(Integer, ForeignKey("folder.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.now)

    # 关系映射
    uploader = relationship("User", backref="files")

    # 级联删除：当文件被删除时，自动删除关联的分享记录
    shares = relationship("Share", back_populates="file", cascade="all, delete-orphan")



    __table_args__ = (
        Index(
            "file_vector_idx",
            "vector_info",
            postgresql_using="hnsw",
            postgresql_ops={"vector_info": "vector_cosine_ops"},
        ),
    )

    def get_abs_path(self):
        """获取文件的绝对路径"""
        return os.path.join(UPLOAD_FOLDER, cast(str, self.file_path))

    def to_dict(self):
        created_at = cast(datetime | None, self.created_at)
        return {
            "id": cast(int | None, self.id),
            "name": cast(str, self.name),
            "status": cast(str | None, self.status),
            "description": cast(str | None, self.description),
            "file_size": cast(int | None, self.file_size),
            "mime_type": cast(str | None, self.mime_type),
            "content_hash": cast(str | None, self.content_hash),
            "uploader_id": cast(int | None, self.uploader_id),
            "parent_id": cast(int | None, self.parent_id),
            "created_at": created_at.isoformat() if created_at else None,
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
            content_hash=d.get("content_hash"),
            uploader_id=d.get("uploader_id"),
            parent_id=d.get("parent_id"),
            created_at=datetime.fromisoformat(cast(str, d.get("created_at")))
            if cast(str | None, d.get("created_at"))
            else None,
        )
