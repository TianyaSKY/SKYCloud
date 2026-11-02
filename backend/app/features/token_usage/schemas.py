"""公开的 Token 记账响应契约与查询过滤条件."""

from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, Field


class TokenStats(BaseModel):
    """单个账户的累计 Token 统计总量."""

    user_id: int
    total_prompt_tokens: int
    total_completion_tokens: int
    total_tokens: int
    last_active_at: str | None


class UserTokenStats(TokenStats):
    """账户身份标识及其累计 Token 统计总量."""

    username: str
    role: Literal["admin", "common"]
    avatar: str | None
    created_at: str | None


class TokenUsageEntry(BaseModel):
    """单条用量审计记录."""

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
    """附带所属账户用户名的用量审计记录."""

    username: str


EntryT = TypeVar("EntryT")


class UsagePage(BaseModel, Generic[EntryT]):
    """单页用量审计记录列表."""

    total: int
    page: int
    page_size: int
    items: list[EntryT]


class DailyStats(BaseModel):
    """单日聚合用量数据统计."""

    date: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    request_count: int


class PerUserDailyStats(DailyStats):
    """单个账户在单日内的聚合用量统计."""

    user_id: int
    username: str


class MyUsageLogsQuery(BaseModel):
    """调用方自身用量审计分页查询的过滤参数."""

    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)
    action: str | None = None
    start_date: str | None = None
    end_date: str | None = None


class UserUsageLogsQuery(BaseModel):
    """查询其他账户用量审计分页的过滤参数."""

    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)
    action: str | None = None
    start_date: str | None = None
    end_date: str | None = None


class AdminAllLogsQuery(BaseModel):
    """全站用量审计分页查询的过滤参数."""

    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)
    action: str | None = None
    user_id: int | None = None
    start_date: str | None = None
    end_date: str | None = None
