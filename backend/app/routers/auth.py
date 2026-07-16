from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse

from app.dependencies import get_current_user
from app.api.schemas.auth import LoginRequest, McpTokenCreateRequest, RegisterRequest
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


@router.post("/auth/mcp-token")
def create_mcp_token(
    payload: McpTokenCreateRequest | None = None,
    current_user=Depends(get_current_user),
):
    """生成 MCP 专用长效 Token（365 天有效期）。

    已登录用户调用此接口获取 Token，配置到 Claude Desktop / Cursor 等 MCP 客户端中。
    """
    result = auth_service.issue_mcp_token(current_user.id, payload.name if payload else None)
    return {**result, "expires_in_days": 365, "usage": "Set as Authorization header: Bearer <mcp_token>"}


@router.get("/auth/mcp-tokens")
def list_mcp_tokens(current_user=Depends(get_current_user)):
    return mcp_token_service.list_mcp_tokens(current_user.id)


@router.delete("/auth/mcp-tokens/{token_id}")
def revoke_mcp_token(token_id: int, current_user=Depends(get_current_user)):
    from app.services import mcp_token_service

    token = mcp_token_service.revoke_mcp_token(current_user.id, token_id)
    return token.to_dict()
