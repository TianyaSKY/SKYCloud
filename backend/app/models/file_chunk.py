"""用于向量检索的文件内容分块索引模型."""

from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import ForeignKey, Index, Integer, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.datetime_utils import utc_now
from app.infra.extensions import Base
from app.infra.utc_datetime import UTCDateTime
from app.models.file import VectorValue


class FileChunk(Base):
    """包含源文件与页码元数据的可检索内容切片."""

    __tablename__ = "file_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    file_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("files.id", ondelete="CASCADE"),
        nullable=False,
    )
    workspace_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    vector_info: Mapped[VectorValue | None] = mapped_column(
        Vector(1024),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "file_id",
            "chunk_index",
            name="uq_file_chunks_file_index",
        ),
        Index(
            "file_chunk_vector_idx",
            "vector_info",
            postgresql_using="hnsw",
            postgresql_ops={"vector_info": "vector_cosine_ops"},
        ),
        Index("idx_file_chunks_ws_file", "workspace_id", "file_id"),
    )
