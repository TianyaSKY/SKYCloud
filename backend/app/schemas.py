from typing import Any

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str
    password: str


class RegisterRequest(BaseModel):
    username: str
    password: str
    avatar: str | None = None


class UserCreateRequest(BaseModel):
    username: str
    password: str
    avatar: str | None = None


class UserUpdateRequest(BaseModel):
    username: str | None = None
    password: str | None = None
    avatar: str | None = None


class UserPasswordUpdateRequest(BaseModel):
    old_password: str
    new_password: str


class FolderCreateRequest(BaseModel):
    name: str
    parent_id: int | None = None


class FolderUpdateRequest(BaseModel):
    name: str | None = None
    parent_id: int | None = None


class FileUpdateRequest(BaseModel):
    name: str | None = None
    status: str | None = None
    parent_id: int | None = None


class BatchDeleteItem(BaseModel):
    id: int
    is_folder: bool = False


class BatchDeleteRequest(BaseModel):
    items: list[BatchDeleteItem] = Field(default_factory=list)


class RetryEmbeddingRequest(BaseModel):
    file_id: int


class MultipartInitRequest(BaseModel):
    filename: str
    total_size: int = Field(gt=0)
    chunk_size: int | None = Field(default=None, gt=0)
    parent_id: int | None = None
    mime_type: str | None = None
    upload_id: str | None = None


class MultipartCompleteRequest(BaseModel):
    upload_id: str


class ShareCreateRequest(BaseModel):
    file_id: int
    expires_at: str | None = None


class SysDictPayload(BaseModel):
    key: str
    value: str
    des: str | None = None
    enable: bool = True


class ChatRequest(BaseModel):
    query: str
    history: list[dict[str, Any]] = Field(default_factory=list)


class AvatarUploadRequest(BaseModel):
    avatar: str  # Base64 string
