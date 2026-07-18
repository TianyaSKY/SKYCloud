"""按领域组织的 API 请求模型。"""

from app.api.schemas.auth import LoginRequest, RegisterRequest
from app.api.schemas.chat import ChatRequest
from app.api.schemas.file import (
    BatchDeleteItem,
    BatchDeleteRequest,
    FilePreflightRequest,
    FileUpdateRequest,
    MultipartCompleteRequest,
    MultipartInitRequest,
    RetryEmbeddingRequest,
)
from app.api.schemas.folder import FolderCreateRequest, FolderUpdateRequest
from app.api.schemas.share import ShareCreateRequest
from app.api.schemas.sys_dict import SysDictPayload
from app.api.schemas.user import (
    UserCreateRequest,
    UserPasswordUpdateRequest,
    UserUpdateRequest,
)
from app.api.schemas.workspace import WorkspaceCreateRequest

__all__ = [
    "BatchDeleteItem", "BatchDeleteRequest", "ChatRequest", "FilePreflightRequest",
    "FileUpdateRequest", "FolderCreateRequest", "FolderUpdateRequest", "LoginRequest",
    "MultipartCompleteRequest", "MultipartInitRequest",
    "RegisterRequest", "RetryEmbeddingRequest", "ShareCreateRequest", "SysDictPayload",
    "UserCreateRequest", "UserPasswordUpdateRequest", "UserUpdateRequest",
    "WorkspaceCreateRequest",
]
