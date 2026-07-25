"""MCP 协议适配层：将 app.features 暴露为 MCP tools/resources/prompts。"""

# 注意：不在包级别急切导入 server，避免与 features 产生循环依赖。
# 入口 entry/mcp_run.py 直接从 app.mcp.server 导入。

__all__ = ["mcp", "get_mcp_app"]


def __getattr__(name: str):
    """延迟导入，按需加载 server 中的对象。"""
    if name in __all__:
        from app.mcp.server import get_mcp_app, mcp  # noqa: F811
        return {"mcp": mcp, "get_mcp_app": get_mcp_app}[name]
    raise AttributeError(f"module 'app.mcp' has no attribute {name!r}")
