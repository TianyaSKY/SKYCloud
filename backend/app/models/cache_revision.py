"""Durable cache namespaces; retained after owner deletion to prevent reuse."""
from sqlalchemy import BigInteger, Column, String
from app.infra.extensions import Base


class SqlCacheRevision(Base):
    __tablename__ = 'sql_cache_revisions'
    scope = Column(String(160), primary_key=True)
    revision = Column(BigInteger, nullable=False, default=0, server_default='0')
