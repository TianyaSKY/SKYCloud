"""工作区相关请求体。"""

from pydantic import BaseModel, ConfigDict, Field


class WorkspaceCreateRequest(BaseModel):
    """创建工作区的 HTTP 请求体。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(..., min_length=1, max_length=120)
