import os
from datetime import datetime
from typing import cast

from pgvector.sqlalchemy import Vector
from sqlalchemy import Column, Integer, String, BigInteger, DateTime, ForeignKey, Index
from sqlalchemy.orm import relationship

from app.infra.extensions import Base, UPLOAD_FOLDER
from app.infra.datetime_utils import beijing_now, local_isoformat


class File(Base):
    """用户文件元数据表：存储路径、AI 描述/向量及索引状态。"""

    __tablename__ = "files"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    file_path = Column(String(512), nullable=False)  # MinIO 对象键
    file_size = Column(BigInteger)  # 字节数，用于容量统计
    mime_type = Column(String(255))  # 如 image/jpeg、application/pdf
    content_hash = Column(String(64))  # 内容 SHA-256，秒传去重

    # ---- AI 索引相关 ----
    # pending / processing / success / fail
    status = Column(String(20), default="pending")
    vector_info = Column(Vector(1024))  # 语义检索向量（余弦距离索引）
    description = Column(String(4096))  # AI 生成的文件描述

    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False, index=True)
    uploader_id = Column(Integer, ForeignKey("users.id"))  # 记录上传者，用于归属标注和通知
    parent_id = Column(Integer, ForeignKey("folder.id"), nullable=True)
    created_at = Column(DateTime, default=beijing_now)

    uploader = relationship("User", backref="files")
    # 文件删除时级联清理分享记录
    shares = relationship("Share", back_populates="file", cascade="all, delete-orphan")
    chunks = relationship("FileChunk", cascade="all, delete-orphan", passive_deletes=True)

    __table_args__ = (
        Index(
            "file_vector_idx",
            "vector_info",
            postgresql_using="hnsw",
            postgresql_ops={"vector_info": "vector_cosine_ops"},
        ),
        Index("idx_files_ws_parent", "workspace_id", "parent_id"),
        Index("idx_files_ws_status", "workspace_id", "status"),
    )

    def get_abs_path(self):
        """兼容旧调用：返回本地临时文件的可能路径，不用于对象存储读取。"""
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
            "workspace_id": cast(int | None, self.workspace_id),
            "uploader_id": cast(int | None, self.uploader_id),
            "parent_id": cast(int | None, self.parent_id),
            "created_at": local_isoformat(created_at),
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
            workspace_id=d.get("workspace_id"),
            uploader_id=d.get("uploader_id"),
            parent_id=d.get("parent_id"),
            created_at=datetime.fromisoformat(cast(str, d.get("created_at")))
            if cast(str | None, d.get("created_at"))
            else None,
        )
