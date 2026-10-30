"""工作空间 API 请求/响应模型."""

from typing import Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field

from app.models.workspace import WorkspaceDict, WorkspaceMemberDict


class WorkspaceCreateRequest(BaseModel):
    """创建工作空间请求."""

    model_config = ConfigDict(str_strip_whitespace=True)
    name: str = Field(..., min_length=1, max_length=120, description="空间名称")
    description: str | None = Field(None, max_length=512, description="空间描述")


class WorkspaceUpdateRequest(BaseModel):
    """更新工作空间请求."""

    name: str | None = Field(None, min_length=1, max_length=128, description="空间名称")
    description: str | None = Field(None, max_length=512, description="空间描述")


class MemberInviteRequest(BaseModel):
    """邀请成员请求."""

    username: str = Field(..., min_length=1, max_length=80, description="被邀请用户名")
    role: str = Field(
        default="viewer",
        pattern=r"^(admin|editor|viewer)$",
        description="角色",
    )


class MemberRoleUpdateRequest(BaseModel):
    """变更成员角色请求."""

    role: str = Field(..., pattern=r"^(admin|editor|viewer)$", description="新角色")


MemberRole = Literal["admin", "editor", "viewer"]
RuntimeStatus = Literal["stopped", "running", "error"]


class WorkspaceResponse(BaseModel):
    """工作空间基础字段."""

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
    """工作空间字段附带当前成员 Runtime 概要."""

    runtime_id: int | None
    user_id: int | None
    config_version: int | None
    access_url: str | None


class WorkspaceListItem(RuntimeWorkspaceResponse):
    """列表中的单个工作空间."""

    my_role: MemberRole
    member_count: int = Field(ge=1)


class WorkspaceListResponse(BaseModel):
    """工作空间列表响应."""

    workspaces: list[WorkspaceListItem]


class MemberResponse(BaseModel):
    """工作空间成员基础字段."""

    id: int = Field(gt=0)
    workspace_id: int = Field(gt=0)
    user_id: int = Field(gt=0)
    role: MemberRole
    invited_by: int | None
    joined_at: str | None


class MemberDetailResponse(MemberResponse):
    """成员字段附带用户展示信息."""

    username: str
    avatar: str | None


class MemberListResponse(BaseModel):
    """成员列表响应."""

    members: list[MemberDetailResponse]


class WorkspaceDetailResponse(RuntimeWorkspaceResponse):
    """工作空间详情响应."""

    my_role: MemberRole
    members: list[MemberDetailResponse]


class UiTicketResponse(BaseModel):
    """Runtime UI 单次使用凭据响应."""

    launch_url: str
    expires_in: int = Field(gt=0)


class WorkspaceMessageResponse(BaseModel):
    """仅携带提示文案的响应."""

    message: str


class MemberDetail(WorkspaceMemberDict):
    """Membership row plus the user's display identity."""

    username: str
    avatar: str | None


class WorkspaceListEntry(WorkspaceDict):
    """Workspace row plus the caller's membership context."""

    my_role: str
    member_count: int


class WorkspaceDetailData(WorkspaceDict):
    """Workspace row plus the caller's role and member list."""

    my_role: str | None
    members: list[MemberDetail]


class RuntimeSummary(TypedDict):
    """The caller's runtime fields merged into a workspace view."""

    runtime_id: int | None
    user_id: int | None
    container_id: str | None
    status: str
    error_message: str | None
    config_version: int | None
    access_url: str | None


class WorkspaceRuntimeData(WorkspaceDict, RuntimeSummary):
    """Workspace fields plus the caller's runtime summary."""


class WorkspaceListItemData(WorkspaceRuntimeData):
    """Workspace list entry including the caller's runtime summary."""

    my_role: str
    member_count: int


class WorkspaceDetailRuntimeData(WorkspaceDetailData, RuntimeSummary):
    """Workspace detail plus the caller's runtime summary."""


class WorkspaceListPayload(TypedDict):
    """The success payload of the workspace list."""

    workspaces: list[WorkspaceListItemData]


class MemberListPayload(TypedDict):
    """The success payload of the member list."""

    members: list[MemberDetail]


class MessagePayload(TypedDict):
    """The success payload carrying only a message."""

    message: str
