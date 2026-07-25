"""认证与用户路由：登录注册、MCP Token 管理、用户资料 CRUD。"""

from fastapi import APIRouter, Depends, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user
from app.features.auth.schemas import (
    LoginRequest,
    RegisterRequest,
    UserCreateRequest,
    UserPasswordUpdateRequest,
    UserUpdateRequest,
)
from app.infra.extensions import get_db
from app.features.auth import service as auth_service
from app.features.auth import user_service

router = APIRouter(tags=["auth"])


@router.post("/auth/login")
def login(payload: LoginRequest, session: Session = Depends(get_db)):
    """用户名密码登录，返回 JWT 与用户信息。"""
    result = auth_service.login(session, payload.username, payload.password)
    return {"message": "Login successful", **result}


@router.post("/auth/register")
def register(payload: RegisterRequest, session: Session = Depends(get_db)):
    """注册新用户（默认普通角色）。"""
    user = auth_service.register_user(
        session, payload.username, payload.password, payload.avatar
    )
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={"message": "User registered successfully", "user": user.to_dict()},
    )


@router.get("/auth/mcp-token")
def get_mcp_token(
    current_user=Depends(get_current_user),
    session: Session = Depends(get_db),
):
    """获取当前用户唯一 MCP Token（无则自动签发，便于客户端复制配置）。"""
    return auth_service.get_mcp_token(session, current_user.id)


@router.post("/auth/mcp-token/refresh")
def refresh_mcp_token(
    current_user=Depends(get_current_user),
    session: Session = Depends(get_db),
):
    """刷新 MCP Token：旧 Token 立即失效，并同步到已运行工作区配置。"""
    return auth_service.refresh_mcp_token(session, current_user.id)


# ---------------------------------------------------------------------------
# 用户资料 CRUD
# ---------------------------------------------------------------------------


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
