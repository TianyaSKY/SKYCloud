"""MCP 协议适配层：将 app.services 暴露为 MCP tools/resources/prompts。"""

from app.mcp.server import get_mcp_app, mcp

__all__ = ["mcp", "get_mcp_app"]
