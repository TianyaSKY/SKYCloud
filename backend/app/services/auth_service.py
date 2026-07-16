import datetime as _dt
import logging
import uuid

import jwt

from app.extensions import SECRET_KEY, db
from app.exceptions import AuthenticationError, BusinessRuleError, ServiceOperationError
from app.models.user import User
from app.services import mcp_token_service
from app.services.user_service import create_user
from app.datetime_utils import beijing_now

logger = logging.getLogger(__name__)


def generate_token(user_id):
    """
    生成 JWT Token
    :param user_id: 用户 ID
    :return: token 字符串
    """
    try:
        payload = {
            "exp": _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=1),
            "iat": _dt.datetime.now(_dt.timezone.utc),
            "sub": str(user_id),
        }
        return jwt.encode(payload, SECRET_KEY, algorithm="HS256")

    except Exception as e:
        logger.error(f"Error generating token: {e}")
        return None


def generate_mcp_token(
    user_id: int, expires_at: _dt.datetime | None = None
) -> str | None:
    """
    生成 MCP 专用长效 JWT Token（365 天有效期）。
    用于 MCP 客户端（Claude Desktop、Cursor 等）的长期配置。

    :param user_id: 用户 ID
    :return: token 字符串
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
        logger.error(f"Error generating MCP token: {e}")
        return None


def decode_token(token):
    """
    解析 JWT Token
    :param token: token 字符串
    :return: 用户 ID 或 错误信息
    """
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        if payload.get("type") == "mcp":
            from app.services import mcp_token_service

            if not mcp_token_service.get_active_mcp_token(token):
                return "MCP token revoked or expired. Please create a new token."
        return payload["sub"]
    except jwt.ExpiredSignatureError:
        return "Token expired. Please log in again."
    except jwt.InvalidTokenError:
        return "Invalid token. Please log in again."


def authenticate_user(username, password):
    """
    验证用户登录
    :param username: 用户名
    :param password: 密码
    :return: token,role 或 None,role
    """
    user = db.session.query(User).filter_by(username=username).first()
    if user and user.check_password(password):
        return generate_token(user.id), user.role, user.id
    return None, "common", None


def login(username: str, password: str) -> dict:
    """验证凭据并生成登录响应所需数据。"""
    if not username or not password:
        raise BusinessRuleError("Missing username or password")
    token, role, user_id = authenticate_user(username, password)
    if not token:
        raise AuthenticationError("Invalid username or password")
    return {"token": token, "role": role, "user_id": user_id}


def register_user(username: str, password: str, avatar: str | None = None) -> User:
    if not username or not password:
        raise BusinessRuleError("Missing username or password")
    try:
        return create_user({"username": username, "password": password, "avatar": avatar})
    except Exception as exc:
        logger.exception("用户注册失败：{}", exc)
        raise BusinessRuleError(str(exc)) from exc


def issue_mcp_token(user_id: int, name: str | None) -> dict:
    expires_at = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=365)
    token = generate_mcp_token(user_id, expires_at)
    if not token:
        raise ServiceOperationError("Failed to generate MCP token")
    token_record = mcp_token_service.create_mcp_token(
        user_id, token, beijing_now() + _dt.timedelta(days=365), name
    )
    return {"mcp_token": token, "token": token_record.to_dict(), "user_id": user_id}
