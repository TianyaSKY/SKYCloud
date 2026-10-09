"""Token 用量路由：个人明细与管理员全站统计。业务在 token_usage_service。"""

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import get_current_user, require_admin
from app.infra.extensions import get_session_factory
from app.features.token_usage import service as token_usage_service
from app.features.auth import user_service

from app.api.contracts import SuccessResponse, success_payload
from app.features.token_usage.schemas import (TokenStats, UserTokenStats, TokenUsageEntry,
    AdminTokenUsageEntry, UsagePage, DailyStats, PerUserDailyStats)

router = APIRouter(tags=["token_usage"])


@router.get("/token-usage/stats", response_model=SuccessResponse[TokenStats])
def my_token_stats(
    current_user=Depends(get_current_user),
    session_factory=Depends(get_session_factory),
):
    """当前用户累计 Token 使用统计。"""
    with session_factory() as session:
        user_service.authorize_account_mutation(session, current_user.id, current_user.id)
        return success_payload(token_usage_service.get_user_token_stats(session, current_user.id))


@router.get("/token-usage/stats/{user_id}", response_model=SuccessResponse[TokenStats])
def user_token_stats(
    user_id: int,
    current_user=Depends(get_current_user),
    session_factory=Depends(get_session_factory),
):
    """指定用户累计统计；仅本人或管理员。"""
    with session_factory() as session:
        user_service.authorize_account_mutation(session, current_user.id, user_id)
        return success_payload(token_usage_service.get_user_token_stats(session, user_id))


@router.get("/token-usage/logs", response_model=SuccessResponse[UsagePage[TokenUsageEntry]])
def my_usage_logs(
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, ge=1, le=100),
        action: str | None = Query(default=None),
        start_date: str | None = Query(default=None),
        end_date: str | None = Query(default=None),
        current_user=Depends(get_current_user),
        session_factory=Depends(get_session_factory),
):
    """当前用户 Token 使用明细分页。"""
    with session_factory() as session:
        user_service.authorize_account_mutation(session, current_user.id, current_user.id)
        return success_payload(token_usage_service.get_usage_logs(
            session=session,
            user_id=current_user.id,
            page=page,
            page_size=page_size,
            action=action,
            start_date=start_date,
            end_date=end_date,
        ))


@router.get("/token-usage/logs/{user_id}", response_model=SuccessResponse[UsagePage[TokenUsageEntry]])
def user_usage_logs(
        user_id: int,
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, ge=1, le=100),
        action: str | None = Query(default=None),
        start_date: str | None = Query(default=None),
        end_date: str | None = Query(default=None),
        current_user=Depends(get_current_user),
        session_factory=Depends(get_session_factory),
):
    """指定用户明细；仅本人或管理员。"""
    with session_factory() as session:
        user_service.authorize_account_mutation(session, current_user.id, user_id)
        return success_payload(token_usage_service.get_usage_logs(
            session=session,
            user_id=user_id,
            page=page,
            page_size=page_size,
            action=action,
            start_date=start_date,
            end_date=end_date,
        ))


@router.get("/token-usage/daily", response_model=SuccessResponse[list[DailyStats]])
def my_daily_stats(
        days: int = Query(default=30, ge=1, le=365),
        current_user=Depends(get_current_user),
        session_factory=Depends(get_session_factory),
):
    """当前用户最近 N 天每日统计。"""
    with session_factory() as session:
        user_service.authorize_account_mutation(session, current_user.id, current_user.id)
        return success_payload(token_usage_service.get_daily_stats(session, current_user.id, days))


@router.get("/token-usage/daily/{user_id}", response_model=SuccessResponse[list[DailyStats]])
def user_daily_stats(
        user_id: int,
        days: int = Query(default=30, ge=1, le=365),
        current_user=Depends(get_current_user),
        session_factory=Depends(get_session_factory),
):
    """指定用户每日统计；仅本人或管理员。"""
    with session_factory() as session:
        user_service.authorize_account_mutation(session, current_user.id, user_id)
        return success_payload(token_usage_service.get_daily_stats(session, user_id, days))


# ---------------------------------------------------------------------------
# 管理员接口（全站视角）
# ---------------------------------------------------------------------------


@router.get("/admin/token-usage/users", response_model=SuccessResponse[list[UserTokenStats]])
def admin_all_users_stats(
    current_user=Depends(require_admin),
    session_factory=Depends(get_session_factory),
):
    """管理员：各用户累计 Token 统计。"""
    with session_factory() as session:
        user_service.authorize_account_mutation(session, current_user.id)
        return success_payload(token_usage_service.get_all_users_token_stats(session))


@router.get("/admin/token-usage/logs", response_model=SuccessResponse[UsagePage[AdminTokenUsageEntry]])
def admin_all_logs(
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, ge=1, le=100),
        action: str | None = Query(default=None),
        user_id: int | None = Query(default=None),
        start_date: str | None = Query(default=None),
        end_date: str | None = Query(default=None),
        current_user=Depends(require_admin),
        session_factory=Depends(get_session_factory),
):
    """管理员：全站 Token 明细分页（可按用户/action 过滤）。"""
    with session_factory() as session:
        user_service.authorize_account_mutation(session, current_user.id)
        return success_payload(token_usage_service.get_all_users_usage_logs(
            session=session,
            page=page,
            page_size=page_size,
            action=action,
            user_id=user_id,
            start_date=start_date,
            end_date=end_date,
        ))


@router.get("/admin/token-usage/daily", response_model=SuccessResponse[list[DailyStats]])
def admin_daily_stats(
        days: int = Query(default=30, ge=1, le=365),
        current_user=Depends(require_admin),
        session_factory=Depends(get_session_factory),
):
    """管理员：全站合计最近 N 天每日统计。"""
    with session_factory() as session:
        user_service.authorize_account_mutation(session, current_user.id)
        return success_payload(token_usage_service.get_all_users_daily_stats(session, days))


@router.get("/admin/token-usage/daily/per-user", response_model=SuccessResponse[list[PerUserDailyStats]])
def admin_per_user_daily_stats(
        days: int = Query(default=30, ge=1, le=365),
        current_user=Depends(require_admin),
        session_factory=Depends(get_session_factory),
):
    """管理员：按用户拆分的最近 N 天每日统计。"""
    with session_factory() as session:
        user_service.authorize_account_mutation(session, current_user.id)
        return success_payload(token_usage_service.get_per_user_daily_stats(session, days))
