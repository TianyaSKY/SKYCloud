"""MCP audit regression tests."""

import asyncio
import json
import pytest
from unittest.mock import patch

from app.mcp.audit import audited_tool
from app.mcp.context import (
    McpRequestContext,
    reset_mcp_context,
    reset_mcp_request_active,
    set_mcp_context,
    set_mcp_request_active,
)


@pytest.mark.parametrize("error_class", [RuntimeError, OSError, ValueError, PermissionError])
def test_unexpected_tool_failure_is_safe_and_audited(error_class):
    private = "PRIVATE_DATABASE_URL_AND_DOCUMENT"

    @audited_tool("broken_tool")
    async def tool():
        raise error_class(private)

    with patch("app.mcp.audit.record_mcp_call") as record:
        with pytest.raises(RuntimeError, match="^Service temporarily unavailable$") as raised:
            asyncio.run(tool())
    assert private not in str(raised.value)
    assert raised.value.__suppress_context__
    assert record.call_args.kwargs["success"] is False
    assert record.call_args.kwargs["error_type"] == error_class.__name__


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


def test_argument_metadata_never_serializes_sensitive_values_or_objects():
    from app.mcp.audit_payload import summarize_call

    class PrivateObject:
        def model_dump(self):
            raise AssertionError("audit must not dump model contents")
        def __str__(self):
            raise AssertionError("audit must not stringify objects")

    def tool(filename, content, token=None, file_id=None, extra=None):
        pass
    secret = "PRIVATE_BODY_AND_TOKEN"
    positional = summarize_call(tool, (secret, secret, secret, 42, PrivateObject()), {})
    keyword = summarize_call(tool, (), {"filename": secret, "content": secret,
                            "token": secret, "file_id": 42, "extra": {secret: secret}})
    for metadata in (positional, keyword):
        assert secret not in metadata.summary
        payload = json.loads(metadata.summary)
        assert payload["schema"] == 2
        assert payload["parameters"]["content"] == {"type": "str", "length": len(secret)}
        assert metadata.entity_id == 42
    invalid = summarize_call(tool, (), {secret: secret})
    assert secret not in invalid.summary and invalid.entity_id is None
    # max_depth is a control, never the entity ID, and bool is never an ID.
    def tree(max_depth):
        pass
    assert summarize_call(tree, (3,), {}).entity_id is None
    assert summarize_call(tool, (secret, secret), {"file_id": True}).entity_id is None


def test_real_audit_transaction_stores_only_metadata(session, test_workspace, test_user, monkeypatch):
    from sqlalchemy.orm import Session
    from app.mcp import audit
    from app.models.mcp_audit_log import McpAuditLog

    monkeypatch.setattr(audit, "SessionLocal", lambda: Session(
        bind=session.get_bind(), join_transaction_mode="create_savepoint"))
    context_token = set_mcp_context(McpRequestContext(
        user_id=test_user.id, workspace_id=test_workspace.id, role="editor"))
    active_token = set_mcp_request_active(True)
    @audited_tool("upload_file")
    async def tool(filename, content, token):
        return '{"success":true}'
    secret = "PRIVATE_DOCUMENT_SENTINEL"
    try:
        assert asyncio.run(tool(secret, secret, token=secret)) == '{"success":true}'
        row = session.query(McpAuditLog).one()
        assert secret not in row.arguments_summary
        assert json.loads(row.arguments_summary)["parameters"]["content"]["length"] == len(secret)
        assert row.success and row.user_id == test_user.id and row.workspace_id == test_workspace.id
    finally:
        reset_mcp_request_active(active_token)
        reset_mcp_context(context_token)


def test_cancelled_tool_is_not_recorded_as_success():
    @audited_tool("cancelled")
    async def tool():
        raise asyncio.CancelledError()
    async def run():
        try:
            await tool()
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("cancelled tool must propagate cancellation")
    with patch("app.mcp.audit.record_mcp_call") as record:
        asyncio.run(run())
    assert record.call_args.kwargs["success"] is False
    assert record.call_args.kwargs["error_type"] == "CancelledError"
