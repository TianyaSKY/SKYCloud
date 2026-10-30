"""Public token accounting response contracts and query filters."""

from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, Field


class TokenStats(BaseModel):
    """Accumulated token totals for one account."""

    user_id: int
    total_prompt_tokens: int
    total_completion_tokens: int
    total_tokens: int
    last_active_at: str | None


class UserTokenStats(TokenStats):
    """Account identity plus its accumulated token totals."""

    username: str
    role: Literal["admin", "common"]
    avatar: str | None
    created_at: str | None


class TokenUsageEntry(BaseModel):
    """One usage audit row."""

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
    """Usage audit row enriched with the owning account name."""

    username: str


EntryT = TypeVar("EntryT")


class UsagePage(BaseModel, Generic[EntryT]):
    """One page of usage audit rows."""

    total: int
    page: int
    page_size: int
    items: list[EntryT]


class DailyStats(BaseModel):
    """One day of aggregated usage."""

    date: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    request_count: int


class PerUserDailyStats(DailyStats):
    """One day of aggregated usage for a single account."""

    user_id: int
    username: str


class MyUsageLogsQuery(BaseModel):
    """Query filters for the caller's usage audit page."""

    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)
    action: str | None = None
    start_date: str | None = None
    end_date: str | None = None


class UserUsageLogsQuery(BaseModel):
    """Query filters for another account's usage audit page."""

    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)
    action: str | None = None
    start_date: str | None = None
    end_date: str | None = None


class AdminAllLogsQuery(BaseModel):
    """Query filters for the site-wide usage audit page."""

    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)
    action: str | None = None
    user_id: int | None = None
    start_date: str | None = None
    end_date: str | None = None
