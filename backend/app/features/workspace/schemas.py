"""工作空间 API 请求/响应模型。"""

from pydantic import BaseModel, Field


class WorkspaceCreateRequest(BaseModel):
    """创建工作空间请求。"""
    name: str = Field(..., min_length=1, max_length=128, description="空间名称")
    description: str | None = Field(None, max_length=512, description="空间描述")


class WorkspaceUpdateRequest(BaseModel):
    """更新工作空间请求。"""
    name: str | None = Field(None, min_length=1, max_length=128, description="空间名称")
    description: str | None = Field(None, max_length=512, description="空间描述")


class MemberInviteRequest(BaseModel):
    """邀请成员请求。"""
    username: str = Field(..., min_length=1, max_length=80, description="被邀请用户名")
    role: str = Field(default="viewer", pattern=r"^(admin|editor|viewer)$", description="角色")


class MemberRoleUpdateRequest(BaseModel):
    """变更成员角色请求。"""
    role: str = Field(..., pattern=r"^(admin|editor|viewer)$", description="新角色")
"""工作区相关请求体。"""

from pydantic import BaseModel, ConfigDict, Field


class WorkspaceCreateRequest(BaseModel):
    """创建工作区的 HTTP 请求体。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(..., min_length=1, max_length=120)
