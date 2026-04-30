"""
MCP Server 独立入口点
作为独立容器运行，使用 Streamable HTTP 传输协议。

使用方式：python mcp_run.py
Docker：command: python mcp_run.py
"""

import logging
import os

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

from app import initialize_application  # noqa: E402
from app.mcp_server import mcp  # noqa: E402

# 初始化数据库连接、表结构等（与 backend-api 的 lifespan 中相同）
initialize_application()
logger.info("MCP Server: Application initialized successfully.")

if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("MCP_PORT", 8001))
    logger.info(f"Starting MCP Server on port {port} (Streamable HTTP)")

    # FastMCP.run() 不支持 host/port 参数，
    # 改用 streamable_http_app() 获取 ASGI 应用后通过 uvicorn 启动
    app = mcp.streamable_http_app()
    uvicorn.run(app, host="0.0.0.0", port=port)
