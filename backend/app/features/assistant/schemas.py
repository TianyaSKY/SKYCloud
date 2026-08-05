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


class AssistantConversationListResponse(BaseModel):
    conversations: list[dict[str, Any]]


class AssistantMessageRequest(BaseModel):
    query: str = Field(min_length=1, max_length=100_000)


class AssistantPermissionResponse(BaseModel):
    response: Literal["once", "reject"]
    remember: bool = False


class AssistantHandoffRequest(BaseModel):
    target_mode: Literal["expert"] = "expert"
    message_id: int | None = None
