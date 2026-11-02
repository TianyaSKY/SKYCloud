"""系统字典写入请求体与公开响应模型."""

from pydantic import BaseModel, Field


class SysDictPayload(BaseModel):
    """字典键值;enable 控制是否生效."""

    key: str = Field(min_length=1, max_length=100)
    value: str = Field(max_length=255)
    des: str | None = Field(default=None, max_length=2048)
    enable: bool = True


class SysDictResponse(BaseModel):
    """公开的数据字典条目投影模型."""

    id: int
    key: str
    value: str
    des: str | None
    enable: bool
    created_at: str | None
