"""Durable cache namespaces; retained after owner deletion to prevent reuse."""
from sqlalchemy import BigInteger, String
from sqlalchemy.orm import Mapped, mapped_column
from app.infra.extensions import Base


class SqlCacheRevision(Base):
    __tablename__ = 'sql_cache_revisions'
    scope: Mapped[str] = mapped_column(String(160), primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default='0')
