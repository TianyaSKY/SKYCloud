"""文件夹创建/更新请求体与响应模型."""

from pydantic import BaseModel, Field


class FolderCreateRequest(BaseModel):
    """创建文件夹; parent_id 为空表示挂到用户根下 (由 service 解析)."""

    name: str = Field(min_length=1, max_length=128)
    parent_id: int | None = Field(default=None, ge=1)


class FolderUpdateRequest(BaseModel):
    """重命名或移动文件夹."""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    parent_id: int | None = Field(default=None, ge=1)


class FolderResponse(BaseModel):
    """单个文件夹的对外表示."""

    id: int = Field(gt=0)
    name: str
    workspace_id: int = Field(gt=0)
    parent_id: int | None
    path: str
    created_at: str | None


class FolderListResponse(BaseModel):
    """工作空间下全部文件夹的扁平列表."""

    folders: list[FolderResponse]


class RootFolderResponse(BaseModel):
    """工作空间根文件夹 ID."""

    root_folder_id: int = Field(gt=0)


class OrganizeResponse(BaseModel):
    """一键整理的入队结果."""

    message: str
    queued: bool
