"""持久化缓存命名空间版本; 在所有者删除后保留以防重用."""

from sqlalchemy import BigInteger, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.extensions import Base


class SqlCacheRevision(Base):
    """基于 SQL 存储的单个缓存命名空间的单调递增版本号."""

    __tablename__ = "sql_cache_revisions"

    scope: Mapped[str] = mapped_column(String(160), primary_key=True)
    revision: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
