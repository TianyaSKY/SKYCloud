"""文件相关请求体：更新、批量删除、分片上传与预检。"""

from pydantic import BaseModel, Field


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
