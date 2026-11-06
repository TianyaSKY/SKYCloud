"""并发 ASGI 请求在下游失败时仍恢复所有上下文数据测试."""

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest
from starlette.types import Message, Receive, Scope, Send

from app.mcp import authentication, authorization, context


async def _dummy_receive() -> Message:
    return {"type": "http.request"}


async def _dummy_send(message: Message) -> None:
    pass


async def test_concurrent_selected_scopes_are_isolated_and_restore_parent_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        authentication, "SessionLocal", MagicMock(side_effect=lambda: MagicMock())
    )

    def resolve(
        session: Any, token: str, selected: str | None
    ) -> context.McpRequestContext:
        return context.McpRequestContext(
            int(token), int(selected or "0"), "editor" if token == "1" else "viewer"
        )

    monkeypatch.setattr(
        authentication.JWTAuthMiddleware, "_resolve_context", staticmethod(resolve)
    )
    all_entered = asyncio.Event()
    entered: list[int] = []
    observed: list[tuple[int, int, str, str | None]] = []
    parent = context.McpRequestContext(90, 91, "admin")
    tokens = (
        context.set_mcp_context(parent),
        context.set_mcp_request_active(active=False),
        context.set_request_id("parent"),
        context.current_user_id.set(90),
    )

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        headers = dict(scope.get("headers", []))  # type: ignore[arg-type]
        auth_bytes = headers.get(b"authorization", b"")
        expected = int(auth_bytes.split()[1])
        entered.append(expected)
        if len(entered) == 2:
            all_entered.set()
        await all_entered.wait()
        selected = context.get_mcp_context()
        observed.append(
            (
                selected.user_id,
                selected.workspace_id,
                selected.role,
                context.get_request_id(),
            )
        )
        assert context.is_mcp_request_active() and context.current_user_id.get() == expected
        assert selected.user_id == expected
        if expected == 2:
            with pytest.raises(PermissionError):
                authorization.require_write_context()
            raise ValueError("downstream failed")
        assert authorization.require_write_context().workspace_id == 101

    middleware = authentication.JWTAuthMiddleware(app)

    async def request(uid: int) -> None:
        scope: Scope = {  # pyright: ignore[reportAssignmentType]
            "type": "http",
            "headers": [
                (b"authorization", f"Bearer {uid}".encode()),
                (b"x-skycloud-workspace-id", str(100 + uid).encode()),
                (b"x-request-id", f"request-{uid}".encode()),
            ],
        }
        try:
            await middleware(scope, _dummy_receive, _dummy_send)
        except ValueError:
            assert uid == 2
        # 在同一协程中观测恢复情况, 而非仅在其父任务中观测.
        assert context.get_mcp_context() is parent
        assert context.get_request_id() == "parent"
        assert not context.is_mcp_request_active()
        assert context.current_user_id.get() == 90

    try:
        await asyncio.wait_for(asyncio.gather(request(1), request(2)), timeout=2)
        assert sorted(observed) == [
            (1, 101, "editor", "request-1"),
            (2, 102, "viewer", "request-2"),
        ]
    finally:
        context.reset_mcp_context(tokens[0])
        context.reset_mcp_request_active(tokens[1])
        context.reset_request_id(tokens[2])
        context.current_user_id.reset(tokens[3])
