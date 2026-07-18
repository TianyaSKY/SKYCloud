"""用户路由：资料 CRUD 与改密。业务在 user_service。"""

from fastapi import APIRouter, Depends, Response, status
from fastapi.responses import JSONResponse

from app.api.dependencies import get_current_user
from app.api.schemas.user import UserCreateRequest, UserPasswordUpdateRequest, UserUpdateRequest
from app.services import user_service

router = APIRouter(tags=["user"])


@router.post("/users")
def create_user(payload: UserCreateRequest):
    """创建用户（公开注册入口之外的管理向创建）。"""
    user = user_service.create_user(payload.model_dump())
    return JSONResponse(status_code=status.HTTP_201_CREATED, content=user.to_dict())


@router.get("/users/{id}")
async def get_user(id: int, current_user=Depends(get_current_user)):
    """获取用户资料；仅本人或管理员。"""
    user_service.ensure_user_access(current_user.id, current_user.role, id)
    user = await user_service.get_user(id)
    return user.to_dict()


@router.put("/users/{id}")
def update_user(
        id: int, payload: UserUpdateRequest, current_user=Depends(get_current_user)
):
    """更新用户资料；仅本人或管理员。"""
    user_service.ensure_user_access(current_user.id, current_user.role, id)
    user = user_service.update_user(id, payload.model_dump(exclude_none=True))
    return user.to_dict()


@router.put("/users/{id}/password")
def update_user_password(
        id: int, payload: UserPasswordUpdateRequest, current_user=Depends(get_current_user)
):
    """修改密码：普通用户需旧密码，管理员可代改。"""
    user_service.ensure_user_access(current_user.id, current_user.role, id)

    user_service.change_password(
        current_user.id, current_user.role, id, payload.old_password, payload.new_password
    )
    return {"message": "Password updated successfully"}


@router.delete("/users/{id}")
def delete_user(id: int, current_user=Depends(get_current_user)):
    """删除用户；仅本人或管理员。"""
    user_service.ensure_user_access(current_user.id, current_user.role, id)
    user_service.delete_user(id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
