"""Pydantic contracts for the assistant API."""

from typing import Any, Literal, TypedDict

from pydantic import BaseModel, Field

from app.models.assistant import AssistantConversationDict, AssistantMessageDict

AssistantMode = Literal["fast", "expert"]


class AssistantConversationCreate(BaseModel):
    """New conversation request."""

    mode: AssistantMode = "fast"
    title: str | None = Field(default=None, max_length=255)


class AssistantConversationUpdate(BaseModel):
    """Partial conversation update request."""

    title: str | None = Field(default=None, min_length=1, max_length=255)
    status: Literal["active", "archived"] | None = None


class AssistantMessageRequest(BaseModel):
    """One chat message submitted for streaming."""

    query: str = Field(min_length=1, max_length=100_000)


class AssistantPermissionResponse(BaseModel):
    """One-shot permission response request."""

    response: Literal["once", "reject"]
    remember: bool = False


class AssistantHandoffRequest(BaseModel):
    """Handoff from a fast conversation to an expert conversation."""

    target_mode: Literal["expert"] = "expert"
    message_id: int | None = None


class AssistantConversationResponse(BaseModel):
    """Public conversation representation."""

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
    """Public message representation."""

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
    """Conversation plus its ordered messages."""

    conversation: AssistantConversationResponse
    messages: list[AssistantMessageResponse]


class AssistantPermissionDetails(BaseModel):
    """Bounded, user-visible permission prompt."""

    permission_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=2000)
    tool: str | None = Field(default=None, max_length=4000)


class AssistantRunResponse(BaseModel):
    """Public run representation."""

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
    """Conversation listing payload."""

    conversations: list[AssistantConversationResponse]


class AssistantConversationListPayload(TypedDict):
    """JSON payload for the conversation listing envelope."""

    conversations: list[AssistantConversationDict]


class AssistantMessagesPayload(TypedDict):
    """JSON payload for the conversation message listing envelope."""

    conversation: AssistantConversationDict
    messages: list[AssistantMessageDict]
