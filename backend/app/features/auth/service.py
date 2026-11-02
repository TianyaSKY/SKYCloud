"""认证与会话:JWT 签发/解析、登录注册,以及 MCP Token 与登录生命周期联动."""

import datetime as dt
import uuid
from collections.abc import Mapping
from typing import TypedDict, cast

import jwt
from loguru import logger
from sqlalchemy.orm import Session

from app.exceptions import AuthenticationError, BusinessRuleError, ServiceOperationError
from app.features.auth import sessions as auth_sessions
from app.features.auth import user_service
from app.features.auth.legacy_admin import LEGACY_ADMIN_HASH
from app.infra.extensions import SECRET_KEY
from app.mcp import runtime_token_service
from app.mcp import token_service as mcp_token_service
from app.models.user import User


def generate_token(
    user_id: int, auth_version: int = 0, *, session_id: str | None = None,
) -> str | None:
    """签发十五分钟访问 JWT;浏览器会话额外绑定可撤销的刷新会话."""
    try:
        payload = {
            "exp": dt.datetime.now(dt.UTC) + dt.timedelta(minutes=15),
            "iat": dt.datetime.now(dt.UTC),
            "sub": str(user_id),
            "ver": auth_version,
        }
        if session_id is not None:
            payload["sid"] = session_id
        return jwt.encode(payload, SECRET_KEY, algorithm="HS256")
    except Exception as e:
        # JWT 签名跨越配置的加密后端。保留
        # 无令牌回退, 且仅记录错误类型, 绝不泄露签名细节。
        logger.error("Access token generation failed: error_type={}", type(e).__name__)
        return None


def generate_mcp_token(
    user_id: int, expires_at: dt.datetime | None = None,
) -> str | None:
    """签发 MCP 专用长效 JWT(默认 365 天),带 jti 以便服务端撤销.

    供 Claude Desktop、Cursor 等 MCP 客户端长期配置,与会话 Token 分离.
    """
    try:
        expiry = expires_at or (dt.datetime.now(dt.UTC) + dt.timedelta(days=365))
        payload = {
            "exp": expiry,
            "iat": dt.datetime.now(dt.UTC),
            "sub": str(user_id),
            "type": "mcp",
            "jti": str(uuid.uuid4()),
        }
        return jwt.encode(payload, SECRET_KEY, algorithm="HS256")
    except Exception as e:
        # 长效令牌签名与访问令牌具有相同的后端异常边界;
        # 客户端绝不能接收原始密钥或密码学库错误细节。
        logger.error("MCP token generation failed: error_type={}", type(e).__name__)
        return None


def decode_token(session: Session, token: str) -> str:
    """解析 JWT;MCP 类型额外查库校验是否已撤销或过期.

    成功返回 sub(用户 ID 字符串),失败返回可展示的错误文案.
    """
    try:
        subject = _decoded_subject(session, token)
    except jwt.ExpiredSignatureError:
        return "Token expired. Please log in again."
    except jwt.InvalidTokenError:
        return "Invalid token. Please log in again."
    else:
        return subject


def _decoded_subject(session: Session, token: str) -> str:
    """在调用方的 JWT 错误边界内解码并鉴权该令牌."""
    payload: Mapping[str, object] = jwt.decode(
        token, SECRET_KEY, algorithms=["HS256"],
    )
    rejection = _token_rejection(session, token, payload)
    if rejection is not None:
        return rejection
    # 本服务签发的令牌始终携带 subject 声明字段。
    return cast("str", payload["sub"])


def _token_rejection(
    session: Session, token: str, payload: Mapping[str, object],
) -> str | None:
    """对于格式合法但业务拒绝的 JWT 返回面向用户的拒绝信息 (若存在)."""
    claim_scope = payload.get("type")
    if claim_scope == "mcp" and not mcp_token_service.get_active_mcp_token(
        session,
        token,
    ):
        return "MCP token revoked or expired. Please create a new token."
    if claim_scope == "mcp_runtime" and not (
        runtime_token_service.validate_runtime_token(session, token)
    ):
        return (
            "MCP runtime token revoked, expired, stopped, or unbound. "
            "Please restart the runtime."
        )
    if claim_scope not in {None, "mcp", "mcp_runtime"}:
        return "Invalid token scope. Please log in again."
    if claim_scope is None and not auth_sessions.valid_session(session, payload):
        return "Session revoked. Please log in again."
    return None


class LoginResult(TypedDict):
    """在浏览器会话创建前返回的凭证校验结果."""

    token: str
    role: str | None
    user_id: int | None


def authenticate_user(
    session: Session, username: str, password: str,
) -> tuple[str | None, str | None, int | None]:
    """校验用户名密码;失败返回 (None, "common", None)."""
    user = session.query(User).filter_by(username=username).first()
    if user and user.role == "admin" and user.password_hash == LEGACY_ADMIN_HASH:
        return None, "common", None
    if user and user.check_password(password):
        return generate_token(user.id, user.auth_version), user.role, user.id
    return None, "common", None


def login(session: Session, username: str, password: str) -> LoginResult:
    """验证凭据并组装登录响应,不自动签发外部 MCP 凭据."""
    if not username or not password:
        message = "Missing username or password"
        raise BusinessRuleError(message)
    token, role, user_id = authenticate_user(session, username, password)
    if not token:
        message = "Invalid username or password"
        raise AuthenticationError(message)
    return {"token": token, "role": role, "user_id": user_id}


def register_user(
    session: Session, username: str, password: str, avatar: str | None = None,
) -> User:
    """注册用户;外部 MCP 凭据由显式签发接口创建."""
    if not username or not password:
        message = "Missing username or password"
        raise BusinessRuleError(message)
    existing = session.query(User).filter_by(username=username).first()
    if existing:
        message = f"Username '{username}' already exists"
        raise BusinessRuleError(message)
    try:
        user = user_service.create_user(
            session, {"username": username, "password": password, "avatar": avatar},
        )
    except Exception as exc:
        session.rollback()
        logger.error("User registration failed: error_type={}", type(exc).__name__)
        message = "User registration failed"
        raise ServiceOperationError(message) from exc
    else:
        return user


def get_mcp_token(session: Session, user_id: int) -> Mapping[str, object]:
    """获取用户 MCP Token 元数据;不签发或返回原始凭据."""
    return mcp_token_service.get_user_mcp_token_payload(session, user_id)


def refresh_mcp_token(session: Session, user_id: int) -> Mapping[str, object]:
    """刷新用户唯一 MCP Token."""
    record, raw = mcp_token_service.refresh_user_mcp_token(session, user_id)
    return {
        "mcp_token": raw,
        "token": record.to_dict(),
        "user_id": user_id,
        "expires_in_days": 365,
        "usage": "Set as Authorization header: Bearer <mcp_token>",
    }


def issue_mcp_token(session: Session, user_id: int) -> Mapping[str, object]:
    """兼容旧接口:等价于 refresh(保证有且只有一个)."""
    return refresh_mcp_token(session, user_id)
