"""MCP 审计日志与追溯回归测试."""

import asyncio
from collections.abc import Callable
import json
from typing import Any
from unittest.mock import Mock, patch

import pytest
from sqlalchemy.orm import Session

from app.mcp import audit
from app.mcp.audit import audited_tool
from app.mcp.audit_payload import AuditArguments, summarize_call
from app.mcp.context import (
    McpRequestContext,
    reset_mcp_context,
    reset_mcp_request_active,
    set_mcp_context,
    set_mcp_request_active,
)
from app.models import McpAuditLog, User, Workspace


@pytest.mark.parametrize(
    "error_class", [RuntimeError, OSError, ValueError, PermissionError]
)
def test_unexpected_tool_failure_is_safe_and_audited(
    error_class: type[Exception],
) -> None:
    private = "PRIVATE_DATABASE_URL_AND_DOCUMENT"

    @audited_tool("broken_tool")
    async def tool() -> None:
        raise error_class(private)

    async def run_tool() -> None:
        await tool()

    with patch("app.mcp.audit.record_mcp_call") as record:
        with pytest.raises(
            RuntimeError, match="^Service temporarily unavailable$"
        ) as raised:
            asyncio.run(run_tool())
    assert private not in str(raised.value)
    assert raised.value.__suppress_context__
    assert record.call_args.kwargs["success"] is False
    assert record.call_args.kwargs["error_type"] == error_class.__name__


def test_audit_marks_structured_tool_errors_as_failures() -> None:
    context_token = set_mcp_context(
        McpRequestContext(user_id=1, workspace_id=2, role="viewer")
    )
    active_token = set_mcp_request_active(active=True)

    @audited_tool("test_tool")
    async def tool() -> str:
        return json.dumps({"error": "denied"})

    async def run_tool() -> str:
        return await tool()

    try:
        with patch("app.mcp.audit.record_mcp_call") as record:
            assert asyncio.run(run_tool()) == json.dumps({"error": "denied"})
        record.assert_called_once()
        assert record.call_args.kwargs["success"] is False
        assert record.call_args.kwargs["error_type"] == "tool_error"
    finally:
        reset_mcp_request_active(active_token)
        reset_mcp_context(context_token)


def test_argument_metadata_never_serializes_sensitive_values_or_objects() -> None:
    class PrivateObject:
        def model_dump(self) -> None:
            raise AssertionError("audit must not dump model contents")

        def __str__(self) -> str:
            raise AssertionError("audit must not stringify objects")

    def tool(
        filename: object,
        content: object,
        token: object = None,
        file_id: object = None,
        extra: object = None,
    ) -> None:
        pass

    secret = "PRIVATE_BODY_AND_TOKEN"
    positional = summarize_call(tool, secret, secret, secret, 42, PrivateObject())
    keyword = summarize_call(
        tool,
        filename=secret,
        content=secret,
        token=secret,
        file_id=42,
        extra={secret: secret},
    )
    for metadata in (positional, keyword):
        assert secret not in metadata.summary
        payload: dict[str, Any] = json.loads(metadata.summary)
        assert payload["schema"] == 2
        assert payload["parameters"]["content"] == {
            "type": "str",
            "length": len(secret),
        }
        assert metadata.entity_id == 42
    invalid = summarize_call(tool, **{secret: secret})
    assert secret not in invalid.summary and invalid.entity_id is None

    # max_depth 是控制参数而非实体 ID, bool 也绝非 ID.
    def tree(max_depth: int) -> None:
        pass

    assert summarize_call(tree, 3).entity_id is None
    assert summarize_call(tool, secret, secret, file_id=True).entity_id is None


def test_real_audit_transaction_stores_only_metadata(
    session: Session,
    test_workspace: Workspace,
    test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        audit,
        "SessionLocal",
        lambda: Session(
            bind=session.get_bind(), join_transaction_mode="create_savepoint"
        ),
    )
    context_token = set_mcp_context(
        McpRequestContext(
            user_id=int(test_user.id),
            workspace_id=int(test_workspace.id),
            role="editor",
        )
    )
    active_token = set_mcp_request_active(active=True)

    @audited_tool("upload_file")
    async def tool(filename: str, content: str, token: str) -> str:
        return '{"success":true}'

    secret = "PRIVATE_DOCUMENT_SENTINEL"

    async def run_tool() -> str:
        return await tool(secret, secret, token=secret)

    try:
        assert (
            asyncio.run(run_tool()) == '{"success":true}'
        )
        row = session.query(McpAuditLog).one()
        assert secret not in str(row.arguments_summary)
        payload: dict[str, Any] = json.loads(str(row.arguments_summary))
        assert payload["parameters"]["content"]["length"] == len(secret)
        assert (
            row.success
            and row.user_id == test_user.id
            and row.workspace_id == test_workspace.id
        )
    finally:
        reset_mcp_request_active(active_token)
        reset_mcp_context(context_token)


def test_cancelled_tool_is_not_recorded_as_success() -> None:
    @audited_tool("cancelled")
    async def tool() -> None:
        raise asyncio.CancelledError()

    async def run() -> None:
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


@pytest.mark.parametrize(
    "faults",
    [
        {"factory"},
        {"add"},
        {"commit"},
        {"commit", "rollback"},
        {"commit", "rollback", "close"},
        {"close"},
    ],
)
def test_audit_session_failures_stay_isolated_and_sanitized(
    monkeypatch: pytest.MonkeyPatch, faults: set[str]
) -> None:
    private = "private-audit-database-and-credential"
    events: list[str] = []
    records: list[str] = []
    session_mock = Mock()

    def operation(name: str) -> Callable[..., None]:
        def perform(*args: Any, **kwargs: Any) -> None:
            events.append(name)
            if name in faults:
                raise RuntimeError(private)

        return perform

    for name in ("add", "commit", "rollback", "close"):
        getattr(session_mock, name).side_effect = operation(name)

    def factory() -> Mock:
        operation("factory")()
        return session_mock

    def log_error(message: object, *args: object) -> None:
        records.append(str(message).format(*args))

    monkeypatch.setattr(audit, "SessionLocal", factory)
    monkeypatch.setattr(audit.logger, "error", log_error)
    context_token = set_mcp_context(McpRequestContext(1, 2, "editor"))
    active_token = set_mcp_request_active(active=True)
    try:
        audit.record_mcp_call(
            tool_name="list_files",
            arguments=AuditArguments("{}", None),
            success=True,
            error_type=None,
            duration_ms=1,
        )
    finally:
        reset_mcp_request_active(active_token)
        reset_mcp_context(context_token)

    assert records and all(private not in record for record in records)
    assert all("error_type=RuntimeError" in record for record in records)
    if "factory" in faults:
        assert events == ["factory"]
    else:
        assert events[-1] == "close"
        session_mock.close.assert_called_once_with()
        if faults & {"add", "commit"}:
            session_mock.rollback.assert_called_once_with()
        else:
            session_mock.rollback.assert_not_called()
