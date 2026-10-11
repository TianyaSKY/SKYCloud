import datetime

from app.infra.utc_datetime import UTCDateTime
from sqlalchemy import Integer, String, Text, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.extensions import Base
from app.infra.datetime_utils import utc_now, local_isoformat


class TokenUsageLog(Base):
    """LLM 调用 Token 明细表：按场景审计用量，并汇总至用户累计字段。"""

    __tablename__ = "token_usage_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    # 调用场景：chat / embedding / rewrite / vl_describe / organize 等
    action: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    model_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int | None] = mapped_column(Integer, default=0)
    # 可选：用户输入摘要（前 200 字符），便于审计
    query_summary: Mapped[str | None] = mapped_column(String(200), default=None)
    # 可选：附加信息，如检索文件数、rerank 结果数等
    extra_info: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime.datetime | None] = mapped_column(UTCDateTime(), default=utc_now)

    def to_dict(self):
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
