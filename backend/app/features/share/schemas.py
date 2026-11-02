"""分享创建请求体与公开响应模型."""

from pydantic import BaseModel, Field


class ShareCreateRequest(BaseModel):
    """为文件创建分享链接;expires_at 为空表示使用默认过期策略."""

    file_id: int = Field(ge=1)
    expires_at: str | None = Field(default=None, max_length=40)


class ShareResponse(BaseModel):
    """公开的分享链接元数据."""

    id: int
    token: str
    file_id: int
    file_name: str | None
    created_at: str | None
    expires_at: str | None
    link: str


class ShareAcknowledgment(BaseModel):
    """取消分享后返回的确认响应模型."""

    message: str
