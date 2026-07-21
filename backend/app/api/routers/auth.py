"""认证路由：登录注册与 MCP 长效 Token 管理。业务在 auth_service。"""

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user
from app.api.schemas.auth import LoginRequest, RegisterRequest
from app.extensions import get_db
from app.services import auth_service

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
