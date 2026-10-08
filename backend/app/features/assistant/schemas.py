"""Pydantic contracts for the assistant API."""

from typing import Any, Literal

from pydantic import BaseModel, Field


AssistantMode = Literal["fast", "expert"]


class AssistantConversationCreate(BaseModel):
    mode: AssistantMode = "fast"
    title: str | None = Field(default=None, max_length=255)


class AssistantConversationUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    status: Literal["active", "archived"] | None = None


class AssistantMessageRequest(BaseModel):
    query: str = Field(min_length=1, max_length=100_000)


class AssistantPermissionResponse(BaseModel):
    response: Literal["once", "reject"]
    remember: bool = False


class AssistantHandoffRequest(BaseModel):
    target_mode: Literal["expert"] = "expert"
    message_id: int | None = None


class AssistantConversationResponse(BaseModel):
    id: int
    workspace_id: int
    user_id: int
    mode: AssistantMode
    title: str | None
    status: Literal['active', 'archived']
    source_conversation_id: int | None
    opencode_runtime_id: int | None
    opencode_session_id: str | None
    created_at: str | None
    updated_at: str | None
    last_message_at: str | None


class AssistantMessageResponse(BaseModel):
    id: int
    conversation_id: int
    role: Literal['user', 'assistant', 'system']
    content: str
    status: Literal['pending', 'streaming', 'completed', 'failed', 'cancelled']
    provider_message_id: str | None
    metadata: dict[str, Any]
    usage: dict[str, Any]
    sequence: int
    created_at: str | None
    updated_at: str | None


class AssistantMessagesResponse(BaseModel):
    conversation: AssistantConversationResponse
    messages: list[AssistantMessageResponse]


class AssistantPermissionDetails(BaseModel):
    permission_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=2000)
    tool: str | None = Field(default=None, max_length=4000)


class AssistantRunResponse(BaseModel):
    id: int
    conversation_id: int
    user_message_id: int | None
    assistant_message_id: int | None
    engine: AssistantMode
    status: Literal['pending', 'running', 'waiting_permission', 'completed', 'failed', 'cancelled']
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
    conversations: list[AssistantConversationResponse]
