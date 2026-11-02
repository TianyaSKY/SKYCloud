"""MCP Server 独立进程入口 (Streamable HTTP + JWT 鉴权).

作为独立容器/进程运行, 所有请求经 Authorization: Bearer <token> 鉴权.
本地: python -m entry.mcp_run
Docker: command: python -m entry.mcp_run
"""

import os

import uvicorn
from app import initialize_application
from app.infra.logging_setup import setup_logging
from app.mcp.server import get_mcp_app, mcp
from loguru import logger

setup_logging()

# 数据库版本只读检查; 数据模式升级操作单独执行。
initialize_application()
logger.info("MCP Server: Application initialized successfully.")

if __name__ == "__main__":
    port = int(os.environ.get("MCP_PORT", "8001"))
    logger.info(
        "Starting MCP Server on port {} (Streamable HTTP + JWT Auth)", port,
    )

    # 获取带 JWT 认证中间件的 ASGI 应用
    app = get_mcp_app(mcp)
    uvicorn.run(app, host="0.0.0.0", port=port, log_config=None)
