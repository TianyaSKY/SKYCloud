"""助手 API 的 Pydantic 契约."""

from typing import Any, Literal, TypedDict

from pydantic import BaseModel, Field

from app.models.assistant import AssistantConversationDict, AssistantMessageDict

AssistantMode = Literal["fast", "expert"]


class AssistantConversationCreate(BaseModel):
    """新建对话请求."""

    mode: AssistantMode = "fast"
    title: str | None = Field(default=None, max_length=255)


class AssistantConversationUpdate(BaseModel):
    """部分更新对话请求."""

    title: str | None = Field(default=None, min_length=1, max_length=255)
    status: Literal["active", "archived"] | None = None


class AssistantMessageRequest(BaseModel):
    """提交用于流式传输的单条聊天消息."""

    query: str = Field(min_length=1, max_length=100_000)


class AssistantPermissionResponse(BaseModel):
    """一次性权限响应请求."""

    response: Literal["once", "reject"]
    remember: bool = False


class AssistantHandoffRequest(BaseModel):
    """从快速对话移交到专家对话."""

    target_mode: Literal["expert"] = "expert"
    message_id: int | None = None


class AssistantConversationResponse(BaseModel):
    """公开对话表示."""

    id: int
    workspace_id: int
    user_id: int
    mode: AssistantMode
    title: str | None
    status: Literal["active", "archived"]
    source_conversation_id: int | None
    opencode_runtime_id: int | None
    opencode_session_id: str | None
    created_at: str | None
    updated_at: str | None
    last_message_at: str | None


class AssistantMessageResponse(BaseModel):
    """公开消息表示."""

    id: int
    conversation_id: int
    role: Literal["user", "assistant", "system"]
    content: str
    status: Literal["pending", "streaming", "completed", "failed", "cancelled"]
    provider_message_id: str | None
    metadata: dict[str, Any]
    usage: dict[str, Any]
    sequence: int
    created_at: str | None
    updated_at: str | None


class AssistantMessagesResponse(BaseModel):
    """对话及其有序消息列表."""

    conversation: AssistantConversationResponse
    messages: list[AssistantMessageResponse]


class AssistantPermissionDetails(BaseModel):
    """有界且对用户可见的权限提示."""

    permission_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=2000)
    tool: str | None = Field(default=None, max_length=4000)


class AssistantRunResponse(BaseModel):
    """公开运行表示."""

    id: int
    conversation_id: int
    user_message_id: int | None
    assistant_message_id: int | None
    engine: AssistantMode
    status: Literal[
        "pending",
        "running",
        "waiting_permission",
        "completed",
        "failed",
        "cancelled",
    ]
    request_id: str | None
    opencode_session_id: str | None
    pending_permission_id: str | None
    pending_permission: AssistantPermissionDetails | None
    cancel_requested: bool
    error_code: str | None
    error_message: str | None
    metadata: dict[str, Any]
    started_at: str | None
    completed_at: str | None
    created_at: str | None


class AssistantConversationListResponse(BaseModel):
    """对话列表负载."""

    conversations: list[AssistantConversationResponse]


class AssistantConversationListPayload(TypedDict):
    """对话列表封套的 JSON 负载."""

    conversations: list[AssistantConversationDict]


class AssistantMessagesPayload(TypedDict):
    """对话消息列表封套的 JSON 负载."""

    conversation: AssistantConversationDict
    messages: list[AssistantMessageDict]
