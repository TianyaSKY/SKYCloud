"""用户文件元数据模型, 包含 AI 描述与向量信息."""

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, TypeAlias, TypedDict

from numpy import float32
from numpy.typing import NDArray
from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infra.datetime_utils import (
    local_isoformat,
    parse_legacy_datetime,
    utc_now,
)
from app.infra.extensions import UPLOAD_FOLDER, Base
from app.infra.utc_datetime import UTCDateTime

if TYPE_CHECKING:
    from app.models.file_chunk import FileChunk
    from app.models.share import Share
    from app.models.user import User

# pgvector 绑定 Python 序列并返回 numpy float32 数组。
VectorValue: TypeAlias = Sequence[float] | NDArray[float32]


class FileDict(TypedDict):
    """文件元数据记录的公开表示模型."""

    id: int
    name: str
    status: str | None
    description: str | None
    file_size: int | None
    mime_type: str | None
    content_hash: str | None
    workspace_id: int
    uploader_id: int | None
    parent_id: int | None
    created_at: str | None


class File(Base):
    """文件元数据模型: 包含存储路径、AI 摘要、特征向量及索引状态."""

    __tablename__ = "files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_path: Mapped[str] = mapped_column(String(512), nullable=False)
    file_size: Mapped[int | None] = mapped_column(BigInteger)
    mime_type: Mapped[str | None] = mapped_column(String(255))
    content_hash: Mapped[str | None] = mapped_column(String(64))

    # ---- AI 索引状态 ----
    # 状态包括待处理、处理中、成功或失败等状态。
    status: Mapped[str | None] = mapped_column(
        String(20),
        default="pending",
        server_default="pending",
    )
    # 语义检索向量 (基于余弦距离索引)。
    vector_info: Mapped[VectorValue | None] = mapped_column(Vector(1024))
    description: Mapped[str | None] = mapped_column(String(4096))

    workspace_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    uploader_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "users.id",
            name="fk_files_uploader_id_users",
            ondelete="SET NULL",
        ),
    )
    parent_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("folder.id"),
        nullable=True,
    )
    created_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        server_default=func.now(),
    )

    uploader: Mapped["User | None"] = relationship("User", backref="files")
    # 分享记录级联随文件一同删除。
    shares: Mapped[list["Share"]] = relationship(
        "Share",
        back_populates="file",
        cascade="all, delete-orphan",
    )
    chunks: Mapped[list["FileChunk"]] = relationship(
        "FileChunk",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        Index("ix_files_object_reference", "file_path"),
        Index(
            "file_vector_idx",
            "vector_info",
            postgresql_using="hnsw",
            postgresql_ops={"vector_info": "vector_cosine_ops"},
        ),
        Index("idx_files_ws_parent", "workspace_id", "parent_id"),
        Index("idx_files_ws_status", "workspace_id", "status"),
        Index("idx_files_content_hash_size", "content_hash", "file_size"),
    )

    def get_abs_path(self) -> str:
        """返回该文件的历史遗留本地上传路径."""
        return str(Path(UPLOAD_FOLDER) / self.file_path)

    def to_dict(self) -> FileDict:
        """返回公开的文件元数据字典表示."""
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "description": self.description,
            "file_size": self.file_size,
            "mime_type": self.mime_type,
            "content_hash": self.content_hash,
            "workspace_id": self.workspace_id,
            "uploader_id": self.uploader_id,
            "parent_id": self.parent_id,
            "created_at": local_isoformat(self.created_at),
        }

    @classmethod
    def from_cache(cls, d: FileDict) -> "File":
        """根据缓存的元数据字段构建游离态文件记录."""
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
            created_at=parse_legacy_datetime(d.get("created_at")),
        )
