from pydantic import BaseModel, Field


class ShareCreateRequest(BaseModel):
    file_id: int = Field(ge=1)
    expires_at: str | None = Field(default=None, max_length=40)
