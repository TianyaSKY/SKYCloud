"""Public token accounting response contracts."""
from typing import Generic, TypeVar, Literal
from pydantic import BaseModel


class TokenStats(BaseModel):
    user_id: int
    total_prompt_tokens: int
    total_completion_tokens: int
    total_tokens: int
    last_active_at: str | None


class UserTokenStats(TokenStats):
    username: str
    role: Literal['admin', 'common']
    avatar: str | None
    created_at: str | None


class TokenUsageEntry(BaseModel):
    id: int
    user_id: int
    action: str
    model_name: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    query_summary: str | None
    extra_info: str | None
    created_at: str | None


class AdminTokenUsageEntry(TokenUsageEntry):
    username: str


EntryT = TypeVar('EntryT')


class UsagePage(BaseModel, Generic[EntryT]):
    total: int
    page: int
    page_size: int
    items: list[EntryT]


class DailyStats(BaseModel):
    date: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    request_count: int


class PerUserDailyStats(DailyStats):
    user_id: int
    username: str
