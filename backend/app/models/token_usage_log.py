"""记录每次 LLM 调用详情并汇总为用户总消耗的 Token 审计模型."""

from datetime import datetime
from typing import TypedDict

from sqlalchemy import ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.datetime_utils import local_isoformat, utc_now
from app.infra.extensions import Base
from app.infra.utc_datetime import UTCDateTime


class TokenUsageLogDict(TypedDict):
    """Token 消耗审计记录的公开表示模型."""

    id: int
    user_id: int
    action: str
    model_name: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    query_summary: str | None
    extra_info: str | None
    created_at: str | None


class TokenUsageLog(Base):
    """每次 LLM 调用审计记录的 Token 消耗明细模型."""

    __tablename__ = "token_usage_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 调用场景: chat / embedding / rewrite / vl_describe / organize 等。
    action: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )
    model_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
    )
    completion_tokens: Mapped[int | None] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
    )
    total_tokens: Mapped[int | None] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
    )
    # 仅供审计的可选截断后用户输入。
    query_summary: Mapped[str | None] = mapped_column(String(200), default=None)
    # 可选附加信息, 如召回文件数或重排结果等。
    extra_info: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        server_default=func.now(),
    )

    __table_args__ = (
        Index("idx_token_usage_logs_created_at", "user_id", "created_at"),
        Index("idx_token_usage_logs_user_action", "user_id", "action"),
        Index("idx_token_usage_logs_user_id", "user_id"),
    )

    def to_dict(self) -> TokenUsageLogDict:
        """返回使用量审计记录的公开字典表示."""
        return {
            "id": self.id,
            "user_id": self.user_id,
            "action": self.action,
            "model_name": self.model_name,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "query_summary": self.query_summary,
            "extra_info": self.extra_info,
            "created_at": local_isoformat(self.created_at),
        }
