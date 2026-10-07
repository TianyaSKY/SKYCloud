"""文件相关请求体：更新、批量删除、分片上传与预检。"""

from typing import Annotated, Literal

from pydantic import BaseModel, Field
from app.features.folder.schemas import FolderResponse


class FileUpdateRequest(BaseModel):
    """文件元数据局部更新。"""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    status: str | None = Field(default=None, max_length=50)
    parent_id: int | None = Field(default=None, ge=1)


class BatchDeleteItem(BaseModel):
    """批量删除中的单项（文件或文件夹）。"""

    id: int = Field(ge=1)
    is_folder: bool = False


class BatchDeleteRequest(BaseModel):
    """批量删除列表，至少一项。"""

    items: list[BatchDeleteItem] = Field(default_factory=list, min_length=1)


class RetryEmbeddingRequest(BaseModel):
    """单文件重新入队索引。"""

    file_id: int = Field(ge=1)


class MultipartInitRequest(BaseModel):
    """分片上传初始化；content_hash 用于秒传判断。"""

    filename: str = Field(min_length=1, max_length=255)
    total_size: int = Field(gt=0)
    chunk_size: int | None = Field(default=None, gt=0)
    parent_id: int | None = Field(default=None, ge=1)
    mime_type: str | None = Field(default=None, max_length=255)
    content_hash: str | None = Field(default=None, min_length=64, max_length=64)
    upload_id: str | None = Field(default=None, max_length=128)


class MultipartCompleteRequest(BaseModel):
    """合并分片并完成上传。"""

    upload_id: str = Field(min_length=1, max_length=128)


class FilePreflightRequest(BaseModel):
    """上传前预检；content_hash 必填以支持秒传。"""

    filename: str = Field(min_length=1, max_length=255)
    total_size: int = Field(gt=0)
    parent_id: int | None = Field(default=None, ge=1)
    mime_type: str | None = Field(default=None, max_length=255)
    content_hash: str = Field(min_length=64, max_length=64)


class FileResponse(BaseModel):
    id: int = Field(gt=0)
    name: str
    status: str | None
    description: str | None
    file_size: int | None = Field(ge=0)
    mime_type: str | None
    content_hash: str | None
    workspace_id: int = Field(gt=0)
    uploader_id: int | None
    parent_id: int | None
    created_at: str | None


class FilePageResponse(BaseModel):
    items: list[FileResponse]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1)


class DirectoryFilePageResponse(FilePageResponse):
    pages: int = Field(ge=0)


class DirectoryResponse(BaseModel):
    folders: list[FolderResponse]
    files: DirectoryFilePageResponse


class PreflightMissResponse(BaseModel):
    instant_upload: Literal[False]
    exists: Literal[False]


class PreflightHitResponse(BaseModel):
    instant_upload: Literal[True]
    exists: Literal[True]
    file: FileResponse


PreflightResponse = Annotated[PreflightMissResponse | PreflightHitResponse, Field(discriminator='instant_upload')]


class MultipartStatusResponse(BaseModel):
    upload_id: str = Field(min_length=1)
    chunk_size: int = Field(gt=0)
    total_chunks: int = Field(gt=0)
    uploaded_chunks: list[Annotated[int, Field(ge=0)]]


class MultipartPendingResponse(MultipartStatusResponse):
    instant_upload: Literal[False]


class MultipartInstantResponse(BaseModel):
    upload_id: None
    chunk_size: int = Field(gt=0)
    total_chunks: int = Field(gt=0)
    uploaded_chunks: list[Annotated[int, Field(ge=0)]]
    instant_upload: Literal[True]
    file: FileResponse


MultipartInitResponse = Annotated[MultipartPendingResponse | MultipartInstantResponse, Field(discriminator='instant_upload')]


class MultipartChunkResponse(BaseModel):
    upload_id: str = Field(min_length=1)
    chunk_index: int = Field(ge=0)
    uploaded_chunks: list[Annotated[int, Field(ge=0)]]


class AvatarResponse(FileResponse):
    avatar_url: str
    avatar: str


class RebuildResponse(BaseModel):
    count: int = Field(ge=0)


class ProcessStatusResponse(BaseModel):
    processing: int = Field(alias='处理中', ge=0)
    success: int = Field(alias='成功', ge=0)
    failed: int = Field(alias='失败', ge=0)
