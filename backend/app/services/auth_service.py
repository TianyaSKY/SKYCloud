import datetime as _dt
import logging
import uuid

import jwt

from app.extensions import SECRET_KEY, db
from app.exceptions import AuthenticationError, BusinessRuleError
from app.models.user import User
from app.services import mcp_token_service
from app.services.user_service import create_user

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
    # 登录时懒初始化唯一 MCP Token（不影响登录结果）
    try:
        mcp_token_service.ensure_user_mcp_token(user_id)
    except Exception:
        logger.exception("登录后初始化 MCP Token 失败：user_id={}", user_id)
    return {"token": token, "role": role, "user_id": user_id}


def register_user(username: str, password: str, avatar: str | None = None) -> User:
    if not username or not password:
        raise BusinessRuleError("Missing username or password")
    try:
        user = create_user({"username": username, "password": password, "avatar": avatar})
        # 注册即自动配置唯一 MCP Token
        try:
            mcp_token_service.ensure_user_mcp_token(user.id)
        except Exception:
            logger.exception("注册后初始化 MCP Token 失败：user_id={}", user.id)
        return user
    except Exception as exc:
        logger.exception("用户注册失败：{}", exc)
        raise BusinessRuleError(str(exc)) from exc


def get_mcp_token(user_id: int) -> dict:
    """获取用户唯一 MCP Token（不存在则自动签发）。"""
    return mcp_token_service.get_user_mcp_token_payload(user_id)


def refresh_mcp_token(user_id: int) -> dict:
    """刷新用户唯一 MCP Token，并同步到所有运行中的工作区。"""
    record, raw = mcp_token_service.refresh_user_mcp_token(user_id)
    # 刷新后把新 Token 注入运行中的工作区，避免工作区仍用旧凭证
    try:
        from app.services import workspace_service

        workspace_service.resync_mcp_for_user(user_id)
    except Exception:
        logger.exception("刷新 MCP Token 后同步工作区失败：user_id={}", user_id)
    return {
        "mcp_token": raw,
        "token": record.to_dict(),
        "user_id": user_id,
        "expires_in_days": 365,
        "usage": "Set as Authorization header: Bearer <mcp_token>",
    }


# 保留旧名兼容（内部若有引用）
def issue_mcp_token(user_id: int, name: str | None = None) -> dict:
    """兼容旧接口：等价于 refresh（保证有且只有一个）。"""
    return refresh_mcp_token(user_id)
