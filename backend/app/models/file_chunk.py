"""文件内容分块索引模型。"""

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, Text, UniqueConstraint
from pgvector.sqlalchemy import Vector

from app.infra.datetime_utils import beijing_now
from app.infra.extensions import Base


class FileChunk(Base):
    """可独立召回的文件内容片段，保留来源文件和页码信息。"""

    __tablename__ = "file_chunks"

    id = Column(Integer, primary_key=True)
    file_id = Column(Integer, ForeignKey("files.id", ondelete="CASCADE"), nullable=False)
    workspace_id = Column(Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    chunk_index = Column(Integer, nullable=False)
    page_number = Column(Integer, nullable=True)
    content = Column(Text, nullable=False)
    vector_info = Column(Vector(1024), nullable=True)
    created_at = Column(DateTime, default=beijing_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("file_id", "chunk_index", name="uq_file_chunks_file_index"),
        Index("file_chunk_vector_idx", "vector_info", postgresql_using="hnsw",
              postgresql_ops={"vector_info": "vector_cosine_ops"}),
        Index("idx_file_chunks_ws_file", "workspace_id", "file_id"),
    )
