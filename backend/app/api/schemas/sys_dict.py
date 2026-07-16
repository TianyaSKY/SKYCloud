from pydantic import BaseModel, Field


class SysDictPayload(BaseModel):
    key: str = Field(min_length=1, max_length=255)
    value: str
    des: str | None = None
    enable: bool = True
