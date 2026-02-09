import datetime
import logging

import jwt

from app.extensions import SECRET_KEY
from app.models.user import User

logger = logging.getLogger(__name__)


def generate_token(user_id):
    """
    生成 JWT Token
    :param user_id: 用户 ID
    :return: token 字符串
    """
    try:
        payload = {
            "exp": datetime.datetime.utcnow() + datetime.timedelta(days=1),
            "iat": datetime.datetime.utcnow(),
            "sub": str(user_id),
        }
        return jwt.encode(payload, SECRET_KEY, algorithm="HS256")

    except Exception as e:
        logger.error(f"Error generating token: {e}")
        return None


def decode_token(token):
    """
    解析 JWT Token
    :param token: token 字符串
    :return: 用户 ID 或 错误信息
    """
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
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
    user = User.query.filter_by(username=username).first()
    if user and user.check_password(password):
        return generate_token(user.id), user.role, user.id
    return None, "common", None
