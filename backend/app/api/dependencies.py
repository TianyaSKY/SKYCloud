"""HTTP 鉴权依赖：JWT 解析、管理员校验、资源归属检查。

支持 Authorization Bearer 与 query ``token``（iframe / SSE 等无法自定义头的场景）。
MCP 专用 JWT 额外校验库内是否仍有效（吊销后立即拒绝）。
"""

import jwt
from fastapi import Depends, HTTPException, status, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.extensions import SECRET_KEY
from app.models.user import User
from app.services import user_service

bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
        token: str | None = Query(default=None, alias="token"),
) -> User:
    """解析当前请求用户；Bearer 优先于 query token。

    MCP type 的 JWT 必须在 mcp_token 表中仍为 active，否则按未授权处理。
    """
    if credentials and credentials.scheme.lower() == "bearer":
        token = credentials.credentials

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token is missing!"
        )

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token!"
            )
        # MCP 长效 token 可被主动吊销，不能只信 JWT 签名
        if payload.get("type") == "mcp":
            from app.services import mcp_token_service

            if not mcp_token_service.get_active_mcp_token(token):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="MCP token is revoked or expired!",
                )
        user_id = int(user_id)
        current_user = await user_service.get_user(user_id)
        if not current_user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found!"
            )

        return current_user
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired!"
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token!"
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token is invalid!"
        ) from exc


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    """要求管理员角色，用于系统字典、全站 Token 用量等接口。"""
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin privilege required"
        )
    return current_user


def ensure_owner_or_admin(current_user: User, owner_id: int) -> None:
    """非资源所有者且非管理员时拒绝，避免越权读写他人数据。"""
    if int(current_user.id) != int(owner_id) and current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied"
        )
