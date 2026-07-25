"""认证与用户相关请求体。"""

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    """用户名密码登录。"""

    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=128)


class RegisterRequest(LoginRequest):
    """注册：在登录字段上可选头像。"""

    avatar: str | None = None


class UserCreateRequest(BaseModel):
    """创建用户。"""

    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=128)
    avatar: str | None = None


class UserUpdateRequest(BaseModel):
    """用户资料局部更新。"""

    username: str | None = Field(default=None, min_length=1, max_length=80)
    password: str | None = Field(default=None, min_length=1, max_length=128)
    avatar: str | None = None


class UserPasswordUpdateRequest(BaseModel):
    """改密：普通用户须提供 old_password。"""

    old_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=1, max_length=128)
