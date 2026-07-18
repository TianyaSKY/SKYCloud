"""MCP Server 独立进程入口（Streamable HTTP + JWT 鉴权）。

作为独立容器/进程运行，所有请求经 Authorization: Bearer <token> 鉴权。
本地：python mcp_run.py
Docker：command: python mcp_run.py
"""

import logging
import os

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

from app import initialize_application  # noqa: E402
from app.mcp.server import mcp, get_mcp_app  # noqa: E402

# 初始化数据库连接、表结构等（与 backend-api lifespan 中相同）
initialize_application()
logger.info("MCP Server: Application initialized successfully.")

if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("MCP_PORT", 8001))
    logger.info(f"Starting MCP Server on port {port} (Streamable HTTP + JWT Auth)")

    # 获取带 JWT 认证中间件的 ASGI 应用
    app = get_mcp_app(mcp)
    uvicorn.run(app, host="0.0.0.0", port=port)
