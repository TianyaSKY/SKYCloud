"""Token 用量路由:个人明细与管理员全站统计.业务在 token_usage_service."""

from collections.abc import Mapping
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.contracts import SuccessEnvelope, SuccessResponse, success_payload
from app.api.dependencies import AdminCtx, Ctx
from app.features.auth import user_service
from app.features.token_usage import service as token_usage_service
from app.features.token_usage.schemas import (
    AdminAllLogsQuery,
    AdminTokenUsageEntry,
    DailyStats,
    MyUsageLogsQuery,
    PerUserDailyStats,
    TokenStats,
    TokenUsageEntry,
    UsagePage,
    UserTokenStats,
    UserUsageLogsQuery,
)

router = APIRouter(tags=["token_usage"])

DaysQuery = Annotated[int, Query(ge=1, le=365)]


@router.get("/token-usage/stats", response_model=SuccessResponse[TokenStats])
def my_token_stats(ctx: Ctx) -> SuccessEnvelope[Mapping[str, object]]:
    """当前用户累计 Token 使用统计."""
    with ctx.session_factory() as session:
        user_service.authorize_account_mutation(session, ctx.user.id, ctx.user.id)
        return success_payload(
            token_usage_service.get_user_token_stats(session, ctx.user.id),
        )


@router.get("/token-usage/stats/{user_id}", response_model=SuccessResponse[TokenStats])
def user_token_stats(
    user_id: int, ctx: Ctx,
) -> SuccessEnvelope[Mapping[str, object]]:
    """指定用户累计统计;仅本人或管理员."""
    with ctx.session_factory() as session:
        user_service.authorize_account_mutation(session, ctx.user.id, user_id)
        return success_payload(
            token_usage_service.get_user_token_stats(session, user_id),
        )


@router.get(
    "/token-usage/logs", response_model=SuccessResponse[UsagePage[TokenUsageEntry]],
)
def my_usage_logs(
    ctx: Ctx,
    query: Annotated[MyUsageLogsQuery, Query()],
) -> SuccessEnvelope[Mapping[str, object]]:
    """当前用户 Token 使用明细分页."""
    with ctx.session_factory() as session:
        user_service.authorize_account_mutation(session, ctx.user.id, ctx.user.id)
        return success_payload(
            token_usage_service.get_usage_logs(
                session,
                token_usage_service.GetUsageLogsParams(
                    user_id=ctx.user.id,
                    page=query.page,
                    page_size=query.page_size,
                    action=query.action,
                    start_date=query.start_date,
                    end_date=query.end_date,
                ),
            ),
        )


@router.get(
    "/token-usage/logs/{user_id}",
    response_model=SuccessResponse[UsagePage[TokenUsageEntry]],
)
def user_usage_logs(
    user_id: int,
    ctx: Ctx,
    query: Annotated[UserUsageLogsQuery, Query()],
) -> SuccessEnvelope[Mapping[str, object]]:
    """指定用户明细;仅本人或管理员."""
    with ctx.session_factory() as session:
        user_service.authorize_account_mutation(session, ctx.user.id, user_id)
        return success_payload(
            token_usage_service.get_usage_logs(
                session,
                token_usage_service.GetUsageLogsParams(
                    user_id=user_id,
                    page=query.page,
                    page_size=query.page_size,
                    action=query.action,
                    start_date=query.start_date,
                    end_date=query.end_date,
                ),
            ),
        )


@router.get("/token-usage/daily", response_model=SuccessResponse[list[DailyStats]])
def my_daily_stats(
    ctx: Ctx, days: DaysQuery = 30,
) -> SuccessEnvelope[list[token_usage_service.DailyStatsPayload]]:
    """当前用户最近 N 天每日统计."""
    with ctx.session_factory() as session:
        user_service.authorize_account_mutation(session, ctx.user.id, ctx.user.id)
        return success_payload(
            token_usage_service.get_daily_stats(session, ctx.user.id, days),
        )


@router.get(
    "/token-usage/daily/{user_id}", response_model=SuccessResponse[list[DailyStats]],
)
def user_daily_stats(
    user_id: int, ctx: Ctx, days: DaysQuery = 30,
) -> SuccessEnvelope[list[token_usage_service.DailyStatsPayload]]:
    """指定用户每日统计;仅本人或管理员."""
    with ctx.session_factory() as session:
        user_service.authorize_account_mutation(session, ctx.user.id, user_id)
        return success_payload(
            token_usage_service.get_daily_stats(session, user_id, days),
        )


# 全站管理员接口


@router.get(
    "/admin/token-usage/users",
    response_model=SuccessResponse[list[UserTokenStats]],
)
def admin_all_users_stats(
    ctx: AdminCtx,
) -> SuccessEnvelope[list[token_usage_service.UserTokenStatsPayload]]:
    """管理员:各用户累计 Token 统计."""
    with ctx.session_factory() as session:
        user_service.authorize_account_mutation(session, ctx.user.id)
        return success_payload(
            token_usage_service.get_all_users_token_stats(session),
        )


@router.get(
    "/admin/token-usage/logs",
    response_model=SuccessResponse[UsagePage[AdminTokenUsageEntry]],
)
def admin_all_logs(
    ctx: AdminCtx,
    query: Annotated[AdminAllLogsQuery, Query()],
) -> SuccessEnvelope[Mapping[str, object]]:
    """管理员:全站 Token 明细分页(可按用户/action 过滤)."""
    with ctx.session_factory() as session:
        user_service.authorize_account_mutation(session, ctx.user.id)
        return success_payload(
            token_usage_service.get_all_users_usage_logs(
                session,
                token_usage_service.AdminUsageLogsParams(
                    page=query.page,
                    page_size=query.page_size,
                    action=query.action,
                    user_id=query.user_id,
                    start_date=query.start_date,
                    end_date=query.end_date,
                ),
            ),
        )


@router.get(
    "/admin/token-usage/daily", response_model=SuccessResponse[list[DailyStats]],
)
def admin_daily_stats(
    ctx: AdminCtx, days: DaysQuery = 30,
) -> SuccessEnvelope[list[token_usage_service.DailyStatsPayload]]:
    """管理员:全站合计最近 N 天每日统计."""
    with ctx.session_factory() as session:
        user_service.authorize_account_mutation(session, ctx.user.id)
        return success_payload(
            token_usage_service.get_all_users_daily_stats(session, days),
        )


@router.get(
    "/admin/token-usage/daily/per-user",
    response_model=SuccessResponse[list[PerUserDailyStats]],
)
def admin_per_user_daily_stats(
    ctx: AdminCtx, days: DaysQuery = 30,
) -> SuccessEnvelope[list[token_usage_service.PerUserDailyStatsPayload]]:
    """管理员:按用户拆分的最近 N 天每日统计."""
    with ctx.session_factory() as session:
        user_service.authorize_account_mutation(session, ctx.user.id)
        return success_payload(
            token_usage_service.get_per_user_daily_stats(session, days),
        )
