"""Request-local HTTP credentials for continued stream authorization."""
from dataclasses import dataclass, field

import jwt

from app.exceptions import AuthenticationError
from app.features.auth.sessions import valid_session
from app.infra.extensions import SECRET_KEY


@dataclass(frozen=True)
class StreamCredential:
    token: str = field(repr=False)
    user_id: int

    def validate(self, session, user_id):
        try:
            payload = jwt.decode(self.token, SECRET_KEY, algorithms=['HS256'])
            if int(payload['sub']) != self.user_id or self.user_id != user_id:
                raise AuthenticationError('Stream session is no longer valid')
            if payload.get('type') == 'mcp':
                # Read-only scalar check avoids cached ORM state and updating
                # last_used_at/committing on every stream poll.
                from app.mcp.token_service import is_active_http_token
                active = is_active_http_token(session, self.token, user_id)
            else:
                active = payload.get('type') is None and valid_session(session, payload)
            if not active:
                raise AuthenticationError('Stream session is no longer valid')
        except (jwt.InvalidTokenError, KeyError, TypeError, ValueError) as exc:
            raise AuthenticationError('Stream session is no longer valid') from exc
