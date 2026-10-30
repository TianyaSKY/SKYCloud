"""认证与用户相关请求体."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class LoginRequest(BaseModel):
    """用户名密码登录."""

    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=128)


class RegisterRequest(LoginRequest):
    """注册:在登录字段上可选头像."""

    avatar: str | None = None


class UserCreateRequest(BaseModel):
    """创建用户."""

    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=128)
    avatar: str | None = None


class UserUpdateRequest(BaseModel):
    """用户资料局部更新;密码只能通过专用改密接口修改."""

    model_config = ConfigDict(extra="forbid")

    username: str | None = Field(default=None, min_length=1, max_length=80)
    avatar: str | None = None


class UserPasswordUpdateRequest(BaseModel):
    """改密:普通用户须提供 old_password."""

    old_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=1, max_length=128)


# Public response models whitelist fields independently of ORM/cache payloads.
class SessionUser(BaseModel):
    """Public account projection embedded in session responses."""

    id: int = Field(gt=0)
    username: str
    role: Literal["admin", "common"]
    avatar: str | None


class UserResponse(SessionUser):
    """Public account profile with accumulated token totals."""

    created_at: str | None
    total_prompt_tokens: int = Field(ge=0)
    total_completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    last_active_at: str | None


class BrowserSessionResponse(BaseModel):
    """Access session returned after login or refresh."""

    token: str = Field(min_length=1)
    user_id: int = Field(gt=0)
    role: Literal["admin", "common"]
    expires_in: int = Field(gt=0)
    user: SessionUser


class LoginResponse(BrowserSessionResponse):
    """Login response adds a human-readable status message."""

    message: str


class MessageResponse(BaseModel):
    """Single human-readable status message."""

    message: str


class RegistrationResponse(MessageResponse):
    """Registration response adds the created account profile."""

    user: UserResponse


class McpTokenMetadata(BaseModel):
    """Public MCP token metadata without the raw credential."""

    id: int = Field(gt=0)
    user_id: int = Field(gt=0)
    name: str | None
    token_preview: str
    created_at: str | None
    expires_at: str | None
    last_used_at: str | None
    revoked_at: str | None
    is_revoked: bool
    is_expired: bool


class McpTokenResponse(BaseModel):
    """Query response; the metadata may be absent before first issuance."""

    token: McpTokenMetadata | None
    user_id: int = Field(gt=0)
    expires_in_days: int = Field(gt=0)
    usage: str


class McpTokenIssueResponse(BaseModel):
    """Issue response keeps the credential and its metadata mandatory."""

    token: McpTokenMetadata
    user_id: int = Field(gt=0)
    expires_in_days: int = Field(gt=0)
    usage: str
    mcp_token: str = Field(min_length=1)
