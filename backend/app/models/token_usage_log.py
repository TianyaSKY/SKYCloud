from datetime import datetime

from sqlalchemy import Column, Integer, String, DateTime, BigInteger, Text

from app.infra.datetime_utils import beijing_now, local_isoformat
from app.extensions import Base


class TokenUsageLog(Base):
    """记录每次 LLM 调用的 token 使用明细"""
    __tablename__ = "token_usage_logs"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    # 调用场景：chat / embedding / rewrite / vl_describe / organize 等
    action = Column(String(50), nullable=False, index=True)
    model_name = Column(String(255), nullable=True)
    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    total_tokens = Column(Integer, default=0)
    # 可选：用户输入摘要（前 200 字符），便于审计
    query_summary = Column(String(200), default=None)
    # 可选：附加信息，如检索文件数、rerank 结果数等
    extra_info = Column(Text, default=None)
    created_at = Column(DateTime, default=beijing_now)

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
