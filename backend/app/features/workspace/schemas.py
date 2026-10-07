"""工作空间 API 请求/响应模型。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class WorkspaceCreateRequest(BaseModel):
    """创建工作空间请求。"""
    model_config = ConfigDict(str_strip_whitespace=True)
    name: str = Field(..., min_length=1, max_length=120, description="空间名称")
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


MemberRole = Literal['admin', 'editor', 'viewer']
RuntimeStatus = Literal['stopped', 'running', 'error']


class WorkspaceResponse(BaseModel):
    id: int = Field(gt=0)
    name: str
    description: str | None
    owner_id: int = Field(gt=0)
    created_at: str | None
    updated_at: str | None
    container_id: str | None
    status: RuntimeStatus
    error_message: str | None


class RuntimeWorkspaceResponse(WorkspaceResponse):
    runtime_id: int | None
    user_id: int | None
    config_version: int | None
    access_url: str | None


class WorkspaceListItem(RuntimeWorkspaceResponse):
    my_role: MemberRole
    member_count: int = Field(ge=1)


class WorkspaceListResponse(BaseModel):
    workspaces: list[WorkspaceListItem]


class MemberResponse(BaseModel):
    id: int = Field(gt=0)
    workspace_id: int = Field(gt=0)
    user_id: int = Field(gt=0)
    role: MemberRole
    invited_by: int | None
    joined_at: str | None


class MemberDetailResponse(MemberResponse):
    username: str
    avatar: str | None


class MemberListResponse(BaseModel):
    members: list[MemberDetailResponse]


class WorkspaceDetailResponse(RuntimeWorkspaceResponse):
    my_role: MemberRole
    members: list[MemberDetailResponse]


class UiTicketResponse(BaseModel):
    launch_url: str
    expires_in: int = Field(gt=0)


class WorkspaceMessageResponse(BaseModel):
    message: str
