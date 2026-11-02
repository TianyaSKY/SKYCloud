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


# 公开响应模型独立于 ORM/缓存载荷建立字段白名单。
class SessionUser(BaseModel):
    """嵌入在会话响应中的公开账户信息模型."""

    id: int = Field(gt=0)
    username: str
    role: Literal["admin", "common"]
    avatar: str | None


class UserResponse(SessionUser):
    """包含累计 Token 统计总量的公开账户画像模型."""

    created_at: str | None
    total_prompt_tokens: int = Field(ge=0)
    total_completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    last_active_at: str | None


class BrowserSessionResponse(BaseModel):
    """登录或刷新令牌后返回的访问会话模型."""

    token: str = Field(min_length=1)
    user_id: int = Field(gt=0)
    role: Literal["admin", "common"]
    expires_in: int = Field(gt=0)
    user: SessionUser


class LoginResponse(BrowserSessionResponse):
    """登录响应模型, 附加可读的状态提示信息."""

    message: str


class MessageResponse(BaseModel):
    """通用的单条可读状态消息模型."""

    message: str


class RegistrationResponse(MessageResponse):
    """注册响应模型, 附加新创建的账户资料."""

    user: UserResponse


class McpTokenMetadata(BaseModel):
    """不含原始凭据明文的公开 MCP 令牌元数据模型."""

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
    """查询响应模型; 在首次签发前元数据可能为空."""

    token: McpTokenMetadata | None
    user_id: int = Field(gt=0)
    expires_in_days: int = Field(gt=0)
    usage: str


class McpTokenIssueResponse(BaseModel):
    """签发响应模型, 凭据明文与元数据均为必填项."""

    token: McpTokenMetadata
    user_id: int = Field(gt=0)
    expires_in_days: int = Field(gt=0)
    usage: str
    mcp_token: str = Field(min_length=1)
