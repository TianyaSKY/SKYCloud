"""OpenCode Server Basic 认证派生.

密码对于特定运行时是确定性的, 但绝不返回给浏览器或存储在数据库中.
当 Docker 启动服务器以及 SKYCloud 调用其内部 HTTP API 时,
使用完全相同的派生算法.
"""

import hashlib
import hmac
import os

from app.infra.extensions import SECRET_KEY
from app.infra.secrets import required_secret


def server_username() -> str:
    """返回配置的提供商用户名或其默认值."""
    return os.getenv("OPENCODE_SERVER_USERNAME", "skycloud")


def server_password(runtime_id: int, user_id: int, workspace_id: int) -> str:
    """为单个运行时身份派生提供商密码."""
    secret = required_secret("OPENCODE_SERVER_SECRET")
    if secret == SECRET_KEY:
        message = "OPENCODE_SERVER_SECRET must differ from SECRET_KEY"
        raise RuntimeError(message)
    material = f"runtime:{runtime_id}:user:{user_id}:workspace:{workspace_id}"
    return hmac.new(secret.encode(), material.encode(), hashlib.sha256).hexdigest()
