"""用户路由：资料 CRUD 与改密。业务在 user_service。"""

from fastapi import APIRouter, Depends, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user
from app.api.schemas.user import UserCreateRequest, UserPasswordUpdateRequest, UserUpdateRequest
from app.extensions import get_db
from app.services import user_service

router = APIRouter(tags=["user"])


@router.post("/users")
def create_user(payload: UserCreateRequest, session: Session = Depends(get_db)):
    """创建用户（公开注册入口之外的管理向创建）。"""
    user = user_service.create_user(session, payload.model_dump())
    return JSONResponse(status_code=status.HTTP_201_CREATED, content=user.to_dict())


@router.get("/users/{id}")
async def get_user(
    id: int,
    current_user=Depends(get_current_user),
    session: Session = Depends(get_db),
):
    """获取用户资料；仅本人或管理员。"""
    user_service.ensure_user_access(current_user.id, current_user.role, id)
    user = await user_service.get_user(session, id)
    return user.to_dict()


@router.put("/users/{id}")
def update_user(
    id: int,
    payload: UserUpdateRequest,
    current_user=Depends(get_current_user),
    session: Session = Depends(get_db),
):
    """更新用户资料；仅本人或管理员。"""
    user_service.ensure_user_access(current_user.id, current_user.role, id)
    user = user_service.update_user(session, id, payload.model_dump(exclude_none=True))
    return user.to_dict()


@router.put("/users/{id}/password")
def update_user_password(
    id: int,
    payload: UserPasswordUpdateRequest,
    current_user=Depends(get_current_user),
    session: Session = Depends(get_db),
):
    """修改密码：普通用户需旧密码，管理员可代改。"""
    user_service.ensure_user_access(current_user.id, current_user.role, id)

    user_service.change_password(
        session,
        current_user.id,
        current_user.role,
        id,
        payload.old_password,
        payload.new_password,
    )
    return {"message": "Password updated successfully"}


@router.delete("/users/{id}")
def delete_user(
    id: int,
    current_user=Depends(get_current_user),
    session: Session = Depends(get_db),
):
    """删除用户；仅本人或管理员。"""
    user_service.ensure_user_access(current_user.id, current_user.role, id)
    user_service.delete_user(session, id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
