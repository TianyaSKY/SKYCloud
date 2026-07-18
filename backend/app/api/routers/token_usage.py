from fastapi import APIRouter, Depends, Query

from app.api.dependencies import get_current_user, require_admin
from app.services import token_usage_service
from app.services import user_service

router = APIRouter(tags=["token_usage"])


@router.get("/token-usage/stats")
def my_token_stats(current_user=Depends(get_current_user)):
    """获取当前用户的累计 token 使用统计"""
    return token_usage_service.get_user_token_stats(current_user.id)


@router.get("/token-usage/stats/{user_id}")
def user_token_stats(user_id: int, current_user=Depends(get_current_user)):
    """获取指定用户的累计 token 使用统计（仅本人或管理员）"""
    user_service.ensure_user_access(current_user.id, current_user.role, user_id)
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
    user_service.ensure_user_access(current_user.id, current_user.role, user_id)
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
    user_service.ensure_user_access(current_user.id, current_user.role, user_id)
    return token_usage_service.get_daily_stats(user_id, days)


# ===================== 管理员接口 =====================


@router.get("/admin/token-usage/users")
def admin_all_users_stats(current_user=Depends(require_admin)):
    """管理员：获取所有用户的累计 token 使用统计"""
    return token_usage_service.get_all_users_token_stats()


@router.get("/admin/token-usage/logs")
def admin_all_logs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    action: str | None = Query(default=None),
    user_id: int | None = Query(default=None),
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    current_user=Depends(require_admin),
):
    """管理员：分页查询所有用户的 token 使用明细"""
    return token_usage_service.get_all_users_usage_logs(
        page=page,
        page_size=page_size,
        action=action,
        user_id=user_id,
        start_date=start_date,
        end_date=end_date,
    )


@router.get("/admin/token-usage/daily")
def admin_daily_stats(
    days: int = Query(default=30, ge=1, le=365),
    current_user=Depends(require_admin),
):
    """管理员：获取所有用户合计最近 N 天的每日统计"""
    return token_usage_service.get_all_users_daily_stats(days)


@router.get("/admin/token-usage/daily/per-user")
def admin_per_user_daily_stats(
    days: int = Query(default=30, ge=1, le=365),
    current_user=Depends(require_admin),
):
    """管理员：获取每个用户最近 N 天的每日统计"""
    return token_usage_service.get_per_user_daily_stats(days)
