"""认证相关请求体：登录与注册。"""

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    """用户名密码登录。"""

    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=128)


class RegisterRequest(LoginRequest):
    """注册：在登录字段上可选头像。"""

    avatar: str | None = None
