"""文件请求体、查询过滤参数与响应数据模型."""

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from app.features.folder.schemas import FolderResponse


class FileUpdateRequest(BaseModel):
    """文件元数据部分更新请求体."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    status: str | None = Field(default=None, max_length=50)
    parent_id: int | None = Field(default=None, ge=1)


class FileListQuery(BaseModel):
    """目录浏览过滤参数; 范围约束沿用既有查询契约."""

    parent_id: int | None = Field(default=None, ge=1)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=10, ge=1, le=100)
    name: str | None = Field(default=None, max_length=255)
    sort_by: str = Field(default="created_at", min_length=1, max_length=50)
    order: str = Field(default="desc", pattern="^(asc|desc)$")


class FileSearchQuery(BaseModel):
    """名称与语义搜索过滤参数; ``type`` 保留既有查询参数名."""

    q: str = Field(default="", max_length=255)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=10, ge=1, le=100)
    search_type: str = Field(
        default="fuzzy", alias="type", pattern="^(fuzzy|semantic)$",
    )


class BatchDeleteItem(BaseModel):
    """批量删除所引用的单个文件或文件夹条目."""

    id: int = Field(ge=1)
    is_folder: bool = False


class BatchDeleteRequest(BaseModel):
    """至少包含一个条目的批量删除请求载荷."""

    items: list[BatchDeleteItem] = Field(
        default_factory=list[BatchDeleteItem], min_length=1,
    )


class RetryEmbeddingRequest(BaseModel):
    """重新入队触发索引生成的单个文件请求."""

    file_id: int = Field(ge=1)


class MultipartInitRequest(BaseModel):
    """分片上传初始化请求; content_hash 支持秒传判定."""

    filename: str = Field(min_length=1, max_length=255)
    total_size: int = Field(gt=0)
    chunk_size: int | None = Field(default=None, gt=0)
    parent_id: int | None = Field(default=None, ge=1)
    mime_type: str | None = Field(default=None, max_length=255)
    content_hash: str | None = Field(default=None, min_length=64, max_length=64)
    upload_id: str | None = Field(default=None, max_length=128)


class MultipartCompleteRequest(BaseModel):
    """分片合并完成请求."""

    upload_id: str = Field(min_length=1, max_length=128)


class FilePreflightRequest(BaseModel):
    """上传预检请求; 必须提供 content_hash 以支持秒传."""

    filename: str = Field(min_length=1, max_length=255)
    total_size: int = Field(gt=0)
    parent_id: int | None = Field(default=None, ge=1)
    mime_type: str | None = Field(default=None, max_length=255)
    content_hash: str = Field(min_length=64, max_length=64)


class FileResponse(BaseModel):
    """各文件接口统一返回的公开文件元数据响应模型."""

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
    """单页文件列表响应模型."""

    items: list[FileResponse]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1)


class DirectoryFilePageResponse(FilePageResponse):
    """嵌入目录浏览响应中的单页文件模型."""

    pages: int = Field(ge=0)


class DirectoryResponse(BaseModel):
    """包含子文件夹列表与单页文件的目录浏览响应."""

    folders: list[FolderResponse]
    files: DirectoryFilePageResponse


class PreflightMissResponse(BaseModel):
    """未命中可秒传对象时的预检响应结果."""

    instant_upload: Literal[False]
    exists: Literal[False]


class PreflightHitResponse(BaseModel):
    """哈希命中已有存储对象时的秒传预检响应."""

    instant_upload: Literal[True]
    exists: Literal[True]
    file: FileResponse


PreflightResponse = Annotated[
    PreflightMissResponse | PreflightHitResponse, Field(discriminator="instant_upload"),
]


class MultipartStatusResponse(BaseModel):
    """进行中的分片上传会话进度响应."""

    upload_id: str = Field(min_length=1)
    chunk_size: int = Field(gt=0)
    total_chunks: int = Field(gt=0)
    uploaded_chunks: list[Annotated[int, Field(ge=0)]]


class MultipartPendingResponse(MultipartStatusResponse):
    """仍需接收后续分片的进行中分片会话响应."""

    instant_upload: Literal[False]


class MultipartInstantResponse(BaseModel):
    """命中已有对象直接完成初始化的分片秒传响应."""

    upload_id: None
    chunk_size: int = Field(gt=0)
    total_chunks: int = Field(gt=0)
    uploaded_chunks: list[Annotated[int, Field(ge=0)]]
    instant_upload: Literal[True]
    file: FileResponse


MultipartInitResponse = Annotated[
    MultipartPendingResponse | MultipartInstantResponse,
    Field(discriminator="instant_upload"),
]


class MultipartChunkResponse(BaseModel):
    """单个上传分片成功持久化后的响应结果."""

    upload_id: str = Field(min_length=1)
    chunk_index: int = Field(ge=0)
    uploaded_chunks: list[Annotated[int, Field(ge=0)]]


class AvatarResponse(FileResponse):
    """已存储的头像文件元数据及其公开分享 URL."""

    avatar_url: str
    avatar: str


class RebuildResponse(BaseModel):
    """重建请求重新入队的文件总数响应."""

    count: int = Field(ge=0)


class ProcessStatusResponse(BaseModel):
    """当前工作空间聚合的文件处理与索引状态响应."""

    processing: int = Field(alias="处理中", ge=0)
    success: int = Field(alias="成功", ge=0)
    failed: int = Field(alias="失败", ge=0)
