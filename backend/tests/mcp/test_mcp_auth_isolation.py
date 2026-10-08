"""Concurrent ASGI requests restore all context fields even on downstream failure."""
import asyncio
from unittest.mock import MagicMock
import pytest
from app.mcp import authentication, authorization, context


async def test_concurrent_selected_scopes_are_isolated_and_restore_parent_on_failure(monkeypatch):
    monkeypatch.setattr(authentication, 'SessionLocal', MagicMock(side_effect=lambda: MagicMock()))
    def resolve(session, token, selected):
        return context.McpRequestContext(int(token), int(selected), 'editor' if token == '1' else 'viewer')
    monkeypatch.setattr(authentication.JWTAuthMiddleware, '_resolve_context', staticmethod(resolve))
    all_entered = asyncio.Event()
    entered = []
    observed = []
    parent = context.McpRequestContext(90, 91, 'admin')
    tokens = (context.set_mcp_context(parent), context.set_mcp_request_active(False),
              context.set_request_id('parent'), context._current_user_id.set(90))
    async def app(scope, receive, send):
        expected = int(scope['headers'][0][1].split()[1])
        entered.append(expected)
        if len(entered) == 2:
            all_entered.set()
        await all_entered.wait()
        selected = context.get_mcp_context()
        observed.append((selected.user_id, selected.workspace_id, selected.role, context.get_request_id()))
        assert context.is_mcp_request_active() and context._current_user_id.get() == expected
        assert selected.user_id == expected
        if expected == 2:
            with pytest.raises(PermissionError):
                authorization._require_write_context()
            raise ValueError('downstream failed')
        assert authorization._require_write_context().workspace_id == 101
    middleware = authentication.JWTAuthMiddleware(app)
    async def request(uid):
        scope = {'type':'http', 'headers':[(b'authorization', f'Bearer {uid}'.encode()),
                 (b'x-skycloud-workspace-id', str(100+uid).encode()), (b'x-request-id', f'request-{uid}'.encode())]}
        try:
            await middleware(scope, None, None)
        except ValueError:
            assert uid == 2
        # Observe restoration in this same coroutine, not just its parent task.
        assert context.get_mcp_context() is parent
        assert context.get_request_id() == 'parent'
        assert not context.is_mcp_request_active()
        assert context._current_user_id.get() == 90
    try:
        await asyncio.wait_for(asyncio.gather(request(1), request(2)), timeout=2)
        assert sorted(observed) == [(1,101,'editor','request-1'), (2,102,'viewer','request-2')]
    finally:
        context.reset_mcp_context(tokens[0]); context.reset_mcp_request_active(tokens[1])
        context.reset_request_id(tokens[2]); context._current_user_id.reset(tokens[3])
