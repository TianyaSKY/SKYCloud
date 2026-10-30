"""MCP 协议适配层: 将 app.features 暴露为 MCP tools/resources/prompts.

不在包级别急切导入 server, 避免与 features 产生循环依赖.
入口 entry/mcp_run.py 直接从 app.mcp.server 导入.
"""
