"""MCP audit regression tests."""

import asyncio
import json
from unittest.mock import patch

from app.mcp.audit import audited_tool
from app.mcp.context import (
    McpRequestContext,
    reset_mcp_context,
    reset_mcp_request_active,
    set_mcp_context,
    set_mcp_request_active,
)


def test_audit_marks_structured_tool_errors_as_failures():
    context_token = set_mcp_context(
        McpRequestContext(user_id=1, workspace_id=2, role="viewer")
    )
    active_token = set_mcp_request_active(True)

    @audited_tool("test_tool")
    async def tool():
        return json.dumps({"error": "denied"})

    try:
        with patch("app.mcp.audit.record_mcp_call") as record:
            assert asyncio.run(tool()) == json.dumps({"error": "denied"})
        record.assert_called_once()
        assert record.call_args.kwargs["success"] is False
        assert record.call_args.kwargs["error_type"] == "tool_error"
    finally:
        reset_mcp_request_active(active_token)
        reset_mcp_context(context_token)
