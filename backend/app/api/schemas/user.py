"""用户资料与改密请求体。"""

from pydantic import BaseModel, Field


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
