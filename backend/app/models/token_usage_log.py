"""LLM token usage audit rows aggregated into per-user totals."""

from datetime import datetime
from typing import TypedDict

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.datetime_utils import local_isoformat, utc_now
from app.infra.extensions import Base
from app.infra.utc_datetime import UTCDateTime


class TokenUsageLogDict(TypedDict):
    """Public representation of a token usage audit row."""

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
    """Token usage detail row audited per LLM call."""

    __tablename__ = "token_usage_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Call scenario: chat / embedding / rewrite / vl_describe / organize, etc.
    action: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        index=True,
    )
    model_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int | None] = mapped_column(Integer, default=0)
    # Optional truncated user input for auditing.
    query_summary: Mapped[str | None] = mapped_column(String(200), default=None)
    # Optional extras such as retrieved file counts or rerank results.
    extra_info: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        default=utc_now,
    )

    def to_dict(self) -> TokenUsageLogDict:
        """Return the public usage audit representation."""
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
