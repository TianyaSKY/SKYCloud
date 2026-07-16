from pydantic import BaseModel, Field


class FileUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    status: str | None = Field(default=None, max_length=50)
    parent_id: int | None = Field(default=None, ge=1)


class BatchDeleteItem(BaseModel):
    id: int = Field(ge=1)
    is_folder: bool = False


class BatchDeleteRequest(BaseModel):
    items: list[BatchDeleteItem] = Field(default_factory=list, min_length=1)


class RetryEmbeddingRequest(BaseModel):
    file_id: int = Field(ge=1)


class MultipartInitRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    total_size: int = Field(gt=0)
    chunk_size: int | None = Field(default=None, gt=0)
    parent_id: int | None = Field(default=None, ge=1)
    mime_type: str | None = Field(default=None, max_length=255)
    content_hash: str | None = Field(default=None, min_length=64, max_length=64)
    upload_id: str | None = Field(default=None, max_length=128)


class MultipartCompleteRequest(BaseModel):
    upload_id: str = Field(min_length=1, max_length=128)


class FilePreflightRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    total_size: int = Field(gt=0)
    parent_id: int | None = Field(default=None, ge=1)
    mime_type: str | None = Field(default=None, max_length=255)
    content_hash: str = Field(min_length=64, max_length=64)
