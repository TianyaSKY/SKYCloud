"""文件夹创建/更新请求体。"""

from pydantic import BaseModel, Field


class FolderCreateRequest(BaseModel):
    """创建文件夹；parent_id 为空表示挂到用户根下（由 service 解析）。"""

    name: str = Field(min_length=1, max_length=255)
    parent_id: int | None = Field(default=None, ge=1)


class FolderUpdateRequest(BaseModel):
    """重命名或移动文件夹。"""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    parent_id: int | None = Field(default=None, ge=1)
