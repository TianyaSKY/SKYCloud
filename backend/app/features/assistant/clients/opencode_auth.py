"""OpenCode Server Basic Auth derivation.

The password is deterministic for a runtime but is never returned to the
browser or stored in the database.  The same derivation is used when Docker
starts the server and when SKYCloud calls its internal HTTP API.
"""

import hashlib
import hmac
import os

from app.infra.extensions import SECRET_KEY


def server_username() -> str:
    return os.getenv("OPENCODE_SERVER_USERNAME", "skycloud")


def server_password(runtime_id: int, user_id: int, workspace_id: int) -> str:
    secret = os.getenv("OPENCODE_SERVER_SECRET") or SECRET_KEY
    material = f"runtime:{runtime_id}:user:{user_id}:workspace:{workspace_id}".encode()
    return hmac.new(secret.encode(), material, hashlib.sha256).hexdigest()

