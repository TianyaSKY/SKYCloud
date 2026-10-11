"""文件内容分块索引模型。"""
import datetime

from app.infra.utc_datetime import UTCDateTime

from sqlalchemy import ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from pgvector.sqlalchemy import Vector

from app.infra.datetime_utils import utc_now
from app.infra.extensions import Base


class FileChunk(Base):
    """可独立召回的文件内容片段，保留来源文件和页码信息。"""

    __tablename__ = "file_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    file_id: Mapped[int] = mapped_column(Integer, ForeignKey("files.id", ondelete="CASCADE"), nullable=False)
    workspace_id: Mapped[int] = mapped_column(Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    vector_info: Mapped[list[float] | None] = mapped_column(Vector(1024), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("file_id", "chunk_index", name="uq_file_chunks_file_index"),
        Index("file_chunk_vector_idx", "vector_info", postgresql_using="hnsw",
              postgresql_ops={"vector_info": "vector_cosine_ops"}),
        Index("idx_file_chunks_ws_file", "workspace_id", "file_id"),
    )
