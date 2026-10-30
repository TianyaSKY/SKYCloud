"""File request bodies, query filters and response models."""

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from app.features.folder.schemas import FolderResponse


class FileUpdateRequest(BaseModel):
    """Partial file metadata update."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    status: str | None = Field(default=None, max_length=50)
    parent_id: int | None = Field(default=None, ge=1)


class FileListQuery(BaseModel):
    """Directory browsing filters; bounds mirror the previous query arguments."""

    parent_id: int | None = Field(default=None, ge=1)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=10, ge=1, le=100)
    name: str | None = Field(default=None, max_length=255)
    sort_by: str = Field(default="created_at", min_length=1, max_length=50)
    order: str = Field(default="desc", pattern="^(asc|desc)$")


class FileSearchQuery(BaseModel):
    """Name/semantic search filters; ``type`` keeps its legacy query name."""

    q: str = Field(default="", max_length=255)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=10, ge=1, le=100)
    search_type: str = Field(
        default="fuzzy", alias="type", pattern="^(fuzzy|semantic)$",
    )


class BatchDeleteItem(BaseModel):
    """One file or folder referenced by a batch delete."""

    id: int = Field(ge=1)
    is_folder: bool = False


class BatchDeleteRequest(BaseModel):
    """Batch delete list holding at least one item."""

    items: list[BatchDeleteItem] = Field(
        default_factory=list[BatchDeleteItem], min_length=1,
    )


class RetryEmbeddingRequest(BaseModel):
    """Single file re-queued for indexing."""

    file_id: int = Field(ge=1)


class MultipartInitRequest(BaseModel):
    """Multipart session start; content_hash enables instant upload."""

    filename: str = Field(min_length=1, max_length=255)
    total_size: int = Field(gt=0)
    chunk_size: int | None = Field(default=None, gt=0)
    parent_id: int | None = Field(default=None, ge=1)
    mime_type: str | None = Field(default=None, max_length=255)
    content_hash: str | None = Field(default=None, min_length=64, max_length=64)
    upload_id: str | None = Field(default=None, max_length=128)


class MultipartCompleteRequest(BaseModel):
    """Chunk assembly request."""

    upload_id: str = Field(min_length=1, max_length=128)


class FilePreflightRequest(BaseModel):
    """Upload preflight; content_hash is required to support instant upload."""

    filename: str = Field(min_length=1, max_length=255)
    total_size: int = Field(gt=0)
    parent_id: int | None = Field(default=None, ge=1)
    mime_type: str | None = Field(default=None, max_length=255)
    content_hash: str = Field(min_length=64, max_length=64)


class FileResponse(BaseModel):
    """Public file metadata returned by every file endpoint."""

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
    """One page of files."""

    items: list[FileResponse]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1)


class DirectoryFilePageResponse(FilePageResponse):
    """File page embedded in a directory listing."""

    pages: int = Field(ge=0)


class DirectoryResponse(BaseModel):
    """Directory listing with its subfolders and file page."""

    folders: list[FolderResponse]
    files: DirectoryFilePageResponse


class PreflightMissResponse(BaseModel):
    """Preflight result when no reusable object exists."""

    instant_upload: Literal[False]
    exists: Literal[False]


class PreflightHitResponse(BaseModel):
    """Preflight result when the hash is already stored."""

    instant_upload: Literal[True]
    exists: Literal[True]
    file: FileResponse


PreflightResponse = Annotated[
    PreflightMissResponse | PreflightHitResponse, Field(discriminator="instant_upload"),
]


class MultipartStatusResponse(BaseModel):
    """Progress of an open multipart session."""

    upload_id: str = Field(min_length=1)
    chunk_size: int = Field(gt=0)
    total_chunks: int = Field(gt=0)
    uploaded_chunks: list[Annotated[int, Field(ge=0)]]


class MultipartPendingResponse(MultipartStatusResponse):
    """Multipart session that still has to receive chunks."""

    instant_upload: Literal[False]


class MultipartInstantResponse(BaseModel):
    """Multipart start satisfied by an existing object."""

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
    """Result of storing one uploaded chunk."""

    upload_id: str = Field(min_length=1)
    chunk_index: int = Field(ge=0)
    uploaded_chunks: list[Annotated[int, Field(ge=0)]]


class AvatarResponse(FileResponse):
    """Stored avatar file plus its share URL."""

    avatar_url: str
    avatar: str


class RebuildResponse(BaseModel):
    """Number of files re-queued by a rebuild request."""

    count: int = Field(ge=0)


class ProcessStatusResponse(BaseModel):
    """Aggregated indexing status for the current workspace."""

    processing: int = Field(alias="处理中", ge=0)
    success: int = Field(alias="成功", ge=0)
    failed: int = Field(alias="失败", ge=0)
