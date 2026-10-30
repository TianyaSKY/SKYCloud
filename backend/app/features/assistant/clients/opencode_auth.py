"""OpenCode Server Basic Auth derivation.

The password is deterministic for a runtime but is never returned to the
browser or stored in the database.  The same derivation is used when Docker
starts the server and when SKYCloud calls its internal HTTP API.
"""

import hashlib
import hmac
import os

from app.infra.extensions import SECRET_KEY
from app.infra.secrets import required_secret


def server_username() -> str:
    """Return the configured provider username or its default."""
    return os.getenv("OPENCODE_SERVER_USERNAME", "skycloud")


def server_password(runtime_id: int, user_id: int, workspace_id: int) -> str:
    """Derive the provider password for one runtime identity."""
    secret = required_secret("OPENCODE_SERVER_SECRET")
    if secret == SECRET_KEY:
        message = "OPENCODE_SERVER_SECRET must differ from SECRET_KEY"
        raise RuntimeError(message)
    material = f"runtime:{runtime_id}:user:{user_id}:workspace:{workspace_id}"
    return hmac.new(secret.encode(), material.encode(), hashlib.sha256).hexdigest()
