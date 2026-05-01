import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse

from app.dependencies import get_current_user
from app.schemas import LoginRequest, McpTokenCreateRequest, RegisterRequest
from app.services.auth_service import authenticate_user, generate_mcp_token
from app.services import mcp_token_service
from app.services.user_service import create_user

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])


@router.post("/auth/login")
def login(payload: LoginRequest):
    if not payload.username or not payload.password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing username or password",
        )

    token, role, user_id = authenticate_user(payload.username, payload.password)
    if token:
        return {
            "token": token,
            "message": "Login successful",
            "role": role,
            "user_id": user_id,
        }

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password"
    )


@router.post("/auth/register")
def register(payload: RegisterRequest):
    if not payload.username or not payload.password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing username or password",
        )

    try:
        user = create_user(payload.model_dump())
        return JSONResponse(
            status_code=status.HTTP_201_CREATED,
            content={"message": "User registered successfully", "user": user.to_dict()},
        )
    except Exception as exc:
        logger.error(f"Registration error: {exc}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc


@router.post("/auth/mcp-token")
def create_mcp_token(
    payload: McpTokenCreateRequest | None = None,
    current_user=Depends(get_current_user),
):
    """生成 MCP 专用长效 Token（365 天有效期）。

    已登录用户调用此接口获取 Token，配置到 Claude Desktop / Cursor 等 MCP 客户端中。
    """
    expires_at = datetime.now(timezone.utc) + timedelta(days=365)
    token = generate_mcp_token(current_user.id, expires_at)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to generate MCP token",
        )
    token_record = mcp_token_service.create_mcp_token(
        current_user.id,
        token,
        expires_at,
        payload.name if payload else None,
    )
    return {
        "mcp_token": token,
        "token": token_record.to_dict(),
        "user_id": current_user.id,
        "expires_in_days": 365,
        "usage": "Set as Authorization header: Bearer <mcp_token>",
    }


@router.get("/auth/mcp-tokens")
def list_mcp_tokens(current_user=Depends(get_current_user)):
    return mcp_token_service.list_mcp_tokens(current_user.id)


@router.delete("/auth/mcp-tokens/{token_id}")
def revoke_mcp_token(token_id: int, current_user=Depends(get_current_user)):
    token = mcp_token_service.revoke_mcp_token(current_user.id, token_id)
    return token.to_dict()
