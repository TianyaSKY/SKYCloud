import logging

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse

from app.dependencies import get_current_user
from app.schemas import LoginRequest, RegisterRequest
from app.services.auth_service import authenticate_user, generate_mcp_token
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
def create_mcp_token(current_user=Depends(get_current_user)):
    """生成 MCP 专用长效 Token（365 天有效期）。

    已登录用户调用此接口获取 Token，配置到 Claude Desktop / Cursor 等 MCP 客户端中。
    """
    token = generate_mcp_token(current_user.id)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to generate MCP token",
        )
    return {
        "mcp_token": token,
        "user_id": current_user.id,
        "expires_in_days": 365,
        "usage": "Set as Authorization header: Bearer <mcp_token>",
    }
