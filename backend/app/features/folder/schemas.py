"""文件夹创建/更新请求体。"""

from pydantic import BaseModel, Field


class FolderCreateRequest(BaseModel):
    """创建文件夹；parent_id 为空表示挂到用户根下（由 service 解析）。"""

    name: str = Field(min_length=1, max_length=128)
    parent_id: int | None = Field(default=None, ge=1)


class FolderUpdateRequest(BaseModel):
    """重命名或移动文件夹。"""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    parent_id: int | None = Field(default=None, ge=1)


class FolderResponse(BaseModel):
    id: int = Field(gt=0)
    name: str
    workspace_id: int = Field(gt=0)
    parent_id: int | None
    path: str
    created_at: str | None


class FolderListResponse(BaseModel):
    folders: list[FolderResponse]


class RootFolderResponse(BaseModel):
    root_folder_id: int = Field(gt=0)


class OrganizeResponse(BaseModel):
    message: str
    queued: bool
