"""Resolve HTTP credentials using one worker-owned SQL unit and detached user data."""
from fastapi import HTTPException
from app.features.auth import account_repository, user_service
from app.models.user import User


def resolve_http_user(session_factory, payload, token):
    with session_factory() as session:
        if payload.get('type') == 'mcp':
            from app.mcp.token_service import get_active_mcp_token
            if not get_active_mcp_token(session, token):
                raise HTTPException(status_code=401, detail='MCP token is revoked or expired!')
        else:
            from app.features.auth.sessions import valid_session
            if not valid_session(session, payload):
                raise HTTPException(status_code=401, detail='Session revoked. Please log in again.')
        user_id = int(payload['sub'])
        role = account_repository.current_role(session, user_id)
        data = user_service._get_user_data(session, user_id)
        if role is None or not data:
            raise HTTPException(status_code=401, detail='User not found!')
        # Do not mutate cached profile data. Authorization role comes from SQL.
        user = User.from_cache({**data, 'role': role})
        from app.api.stream_credentials import StreamCredential
        # Only the request-local detached object carries the raw credential.
        # It is never put in cached profile data or User.to_dict().
        user._http_stream_credential = StreamCredential(token, user_id)
        return user
