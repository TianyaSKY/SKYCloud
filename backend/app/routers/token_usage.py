from fastapi import APIRouter, Depends, Query

from app.dependencies import get_current_user, ensure_owner_or_admin, require_admin
from app.services import token_usage_service

router = APIRouter(tags=["token_usage"])


@router.get("/token-usage/stats")
def my_token_stats(current_user=Depends(get_current_user)):
    """获取当前用户的累计 token 使用统计"""
    return token_usage_service.get_user_token_stats(current_user.id)


@router.get("/token-usage/stats/{user_id}")
def user_token_stats(user_id: int, current_user=Depends(get_current_user)):
    """获取指定用户的累计 token 使用统计（仅本人或管理员）"""
    ensure_owner_or_admin(current_user, user_id)
    return token_usage_service.get_user_token_stats(user_id)


@router.get("/token-usage/logs")
def my_usage_logs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    action: str | None = Query(default=None),
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    current_user=Depends(get_current_user),
):
    """获取当前用户的 token 使用明细（分页）"""
    return token_usage_service.get_usage_logs(
        user_id=current_user.id,
        page=page,
        page_size=page_size,
        action=action,
        start_date=start_date,
        end_date=end_date,
    )


@router.get("/token-usage/logs/{user_id}")
def user_usage_logs(
    user_id: int,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    action: str | None = Query(default=None),
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    current_user=Depends(get_current_user),
):
    """获取指定用户的 token 使用明细（仅本人或管理员）"""
    ensure_owner_or_admin(current_user, user_id)
    return token_usage_service.get_usage_logs(
        user_id=user_id,
        page=page,
        page_size=page_size,
        action=action,
        start_date=start_date,
        end_date=end_date,
    )


@router.get("/token-usage/daily")
def my_daily_stats(
    days: int = Query(default=30, ge=1, le=365),
    current_user=Depends(get_current_user),
):
    """获取当前用户最近 N 天的每日统计"""
    return token_usage_service.get_daily_stats(current_user.id, days)


@router.get("/token-usage/daily/{user_id}")
def user_daily_stats(
    user_id: int,
    days: int = Query(default=30, ge=1, le=365),
    current_user=Depends(get_current_user),
):
    """获取指定用户最近 N 天的每日统计（仅本人或管理员）"""
    ensure_owner_or_admin(current_user, user_id)
    return token_usage_service.get_daily_stats(user_id, days)
