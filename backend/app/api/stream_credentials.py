"""用于流式响应持续鉴权的请求局部 HTTP 凭证."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

import jwt

from app.exceptions import AuthenticationError
from app.features.auth import sessions
from app.infra.extensions import SECRET_KEY
from app.mcp import token_service

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sqlalchemy.orm import Session

_STREAM_INVALID = "Stream session is no longer valid"


@dataclass(frozen=True)
class StreamCredential:
    """用于长生命周期数据流持续重新验证的请求局部凭据."""

    token: str = field(repr=False)
    user_id: int

    def validate(self, session: Session, user_id: int) -> None:
        """根据当前权威数据库状态验证流凭据的有效性."""
        try:
            payload = cast(
                "Mapping[str, object]",
                jwt.decode(self.token, SECRET_KEY, algorithms=["HS256"]),
            )
            if (
                int(cast("str | int", payload["sub"])) != self.user_id
                or self.user_id != user_id
            ):
                raise AuthenticationError(_STREAM_INVALID)
            if payload.get("type") == "mcp":
                # 只读标量检查避免了缓存的 ORM 状态,
                # 并避免每次轮询时更新 last_used_at / 提交事务。
                active = token_service.is_active_http_token(
                    session, self.token, user_id,
                )
            else:
                active = (
                    payload.get("type") is None
                    and sessions.valid_session(session, payload)
                )
            if not active:
                raise AuthenticationError(_STREAM_INVALID)
        except (jwt.InvalidTokenError, KeyError, TypeError, ValueError) as exc:
            message = _STREAM_INVALID
            raise AuthenticationError(message) from exc
