from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse

from app.api.dependencies import get_current_user
from app.api.schemas.auth import LoginRequest, RegisterRequest
from app.services import auth_service

router = APIRouter(tags=["auth"])


@router.post("/auth/login")
def login(payload: LoginRequest):
    result = auth_service.login(payload.username, payload.password)
    return {"message": "Login successful", **result}


@router.post("/auth/register")
def register(payload: RegisterRequest):
    user = auth_service.register_user(payload.username, payload.password, payload.avatar)
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={"message": "User registered successfully", "user": user.to_dict()},
    )


@router.get("/auth/mcp-token")
def get_mcp_token(current_user=Depends(get_current_user)):
    """获取当前用户唯一 MCP Token（自动签发；可复制）。"""
    return auth_service.get_mcp_token(current_user.id)


@router.post("/auth/mcp-token/refresh")
def refresh_mcp_token(current_user=Depends(get_current_user)):
    """刷新唯一 MCP Token（旧 Token 立即失效，并同步到运行中工作区）。"""
    return auth_service.refresh_mcp_token(current_user.id)
