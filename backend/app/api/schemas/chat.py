"""对话请求体。"""

from typing import Any

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """RAG 对话：查询文本与可选多轮历史。"""

    query: str = Field(min_length=1)
    history: list[dict[str, Any]] = Field(default_factory=list)
