"""认证与会话：JWT 签发/解析、登录注册，以及 MCP Token 与登录生命周期联动。"""

import datetime as _dt
import uuid

import jwt
from loguru import logger
from sqlalchemy.orm import Session

from app.exceptions import AuthenticationError, BusinessRuleError, ServiceOperationError
from app.infra.extensions import SECRET_KEY
from app.models.user import User
from app.mcp import token_service as mcp_token_service
from app.features.auth.user_service import create_user


def generate_token(user_id, auth_version: int = 0, *, session_id: str | None = None):
    """签发十五分钟访问 JWT；浏览器会话额外绑定可撤销的刷新会话。"""
    try:
        payload = {
            "exp": _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(minutes=15),
            "iat": _dt.datetime.now(_dt.timezone.utc),
            "sub": str(user_id),
            "ver": auth_version,
        }
        if session_id is not None:
            payload['sid'] = session_id
        return jwt.encode(payload, SECRET_KEY, algorithm="HS256")

    except Exception as e:
        logger.error("Access token generation failed: error_type={}", type(e).__name__)
        return None


def generate_mcp_token(
        user_id: int, expires_at: _dt.datetime | None = None
) -> str | None:
    """签发 MCP 专用长效 JWT（默认 365 天），带 jti 以便服务端撤销。

    供 Claude Desktop、Cursor 等 MCP 客户端长期配置，与会话 Token 分离。
    """
    try:
        expires_at = expires_at or (
                _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=365)
        )
        payload = {
            "exp": expires_at,
            "iat": _dt.datetime.now(_dt.timezone.utc),
            "sub": str(user_id),
            "type": "mcp",
            "jti": str(uuid.uuid4()),
        }
        return jwt.encode(payload, SECRET_KEY, algorithm="HS256")
    except Exception as e:
        logger.error("MCP token generation failed: error_type={}", type(e).__name__)
        return None


def decode_token(session: Session, token):
    """解析 JWT；MCP 类型额外查库校验是否已撤销或过期。

    成功返回 sub（用户 ID 字符串），失败返回可展示的错误文案。
    """
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        if payload.get("type") == "mcp":
            from app.mcp import token_service as mcp_token_service

            if not mcp_token_service.get_active_mcp_token(session, token):
                return "MCP token revoked or expired. Please create a new token."
        elif payload.get("type") == "mcp_runtime":
            from app.mcp import runtime_token_service

            if not runtime_token_service.validate_runtime_token(session, token):
                return (
                    "MCP runtime token revoked, expired, stopped, or unbound. "
                    "Please restart the runtime."
                )
        if payload.get("type") not in {None, "mcp", "mcp_runtime"}:
            return "Invalid token scope. Please log in again."
        if payload.get("type") is None:
            from app.features.auth.sessions import valid_session
            if not valid_session(session, payload):
                return "Session revoked. Please log in again."
        return payload["sub"]
    except jwt.ExpiredSignatureError:
        return "Token expired. Please log in again."
    except jwt.InvalidTokenError:
        return "Invalid token. Please log in again."


def authenticate_user(session: Session, username, password):
    """校验用户名密码；成功返回 (token, role, user_id)，失败返回 (None, "common", None)。"""
    user = session.query(User).filter_by(username=username).first()
    from app.features.auth.legacy_admin import LEGACY_ADMIN_HASH
    if user and user.role == "admin" and user.password_hash == LEGACY_ADMIN_HASH:
        return None, "common", None
    if user and user.check_password(password):
        return generate_token(user.id, user.auth_version), user.role, user.id
    return None, "common", None


def login(session: Session, username: str, password: str) -> dict:
    """验证凭据并组装登录响应，不自动签发外部 MCP 凭据。"""
    if not username or not password:
        raise BusinessRuleError("Missing username or password")
    token, role, user_id = authenticate_user(session, username, password)
    if not token:
        raise AuthenticationError("Invalid username or password")
    return {"token": token, "role": role, "user_id": user_id}


def register_user(session: Session, username: str, password: str, avatar: str | None = None) -> User:
    """注册用户；外部 MCP 凭据由显式签发接口创建。"""
    if not username or not password:
        raise BusinessRuleError("Missing username or password")
    existing = session.query(User).filter_by(username=username).first()
    if existing:
        raise BusinessRuleError(f"Username '{username}' already exists")
    try:
        user = create_user(session, {"username": username, "password": password, "avatar": avatar})
        return user
    except Exception as exc:
        session.rollback()
        logger.error("User registration failed: error_type={}", type(exc).__name__)
        raise ServiceOperationError("User registration failed") from exc


def get_mcp_token(session: Session, user_id: int) -> dict:
    """获取用户 MCP Token 元数据；不签发或返回原始凭据。"""
    return mcp_token_service.get_user_mcp_token_payload(session, user_id)


def refresh_mcp_token(session: Session, user_id: int) -> dict:
    """刷新用户唯一 MCP Token。"""
    record, raw = mcp_token_service.refresh_user_mcp_token(session, user_id)
    return {
        "mcp_token": raw,
        "token": record.to_dict(),
        "user_id": user_id,
        "expires_in_days": 365,
        "usage": "Set as Authorization header: Bearer <mcp_token>",
    }


# 保留旧名兼容（内部若有引用）
def issue_mcp_token(session: Session, user_id: int, name: str | None = None) -> dict:
    """兼容旧接口：等价于 refresh（保证有且只有一个）。"""
    return refresh_mcp_token(session, user_id)
