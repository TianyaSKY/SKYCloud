"""Resolve HTTP credentials using one worker-owned SQL unit and detached user data."""

from collections.abc import Mapping
from typing import cast

from fastapi import HTTPException

from app.api.stream_credentials import StreamCredential
from app.features.auth import account_repository, sessions, user_service
from app.infra.extensions import SessionFactory
from app.mcp import token_service
from app.models.user import User, UserDict

_MCP_REVOKED = "MCP token is revoked or expired!"
_SESSION_REVOKED = "Session revoked. Please log in again."
_USER_MISSING = "User not found!"


def resolve_http_user(
    session_factory: SessionFactory,
    payload: Mapping[str, object],
    token: str,
) -> User:
    """Resolve a token into a detached user inside the caller's worker thread."""
    with session_factory() as session:
        if payload.get("type") == "mcp":
            if not token_service.get_active_mcp_token(session, token):
                raise HTTPException(status_code=401, detail=_MCP_REVOKED)
        elif not sessions.valid_session(session, payload):
            raise HTTPException(status_code=401, detail=_SESSION_REVOKED)
        user_id = int(cast("str | int", payload["sub"]))
        role = account_repository.current_role(session, user_id)
        data = user_service.get_user_data(session, user_id)
        if role is None or not data:
            raise HTTPException(status_code=401, detail=_USER_MISSING)
        # Do not mutate cached profile data. Authorization role comes from SQL.
        user = User.from_cache(cast("UserDict", {**data, "role": role}))
        # Only the request-local detached object carries the raw credential.
        # It is never put in cached profile data or User.to_dict().
        user.http_stream_credential = StreamCredential(token, user_id)
        return user
