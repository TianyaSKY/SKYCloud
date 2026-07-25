"""系统字典写入请求体。"""

from pydantic import BaseModel, Field


class SysDictPayload(BaseModel):
    """字典键值；enable 控制是否生效。"""

    key: str = Field(min_length=1, max_length=255)
    value: str
    des: str | None = None
    enable: bool = True
