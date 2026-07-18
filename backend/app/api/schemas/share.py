"""分享创建请求体。"""

from pydantic import BaseModel, Field


class ShareCreateRequest(BaseModel):
    """为文件创建分享链接；expires_at 为空表示使用默认过期策略。"""

    file_id: int = Field(ge=1)
    expires_at: str | None = Field(default=None, max_length=40)
