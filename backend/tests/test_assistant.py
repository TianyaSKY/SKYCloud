"""Assistant persistence, event mapping, and security regression tests."""

import asyncio
from unittest.mock import patch

import jwt

from app.features.assistant import repository, service
from app.features.assistant.clients.opencode_auth import server_password
from app.features.assistant.clients.opencode_events import map_opencode_event
from app.features.assistant.engines.fast_engine import FastEngine
from app.features.assistant.event_protocol import AssistantEvent
from app.infra.extensions import SECRET_KEY
from app.models.assistant import AssistantMessage
from app.models.user import User
from app.models.workspace import WorkspaceMember


def test_assistant_repository_persists_mode_specific_records(session, test_user, test_workspace):
    conversation = repository.create_conversation(
        session, test_workspace.id, test_user.id, "fast", "资料查询"
    )
    user_message = repository.create_message(session, conversation, "user", "查找退款规则")
    assistant_message = repository.create_message(
        session, conversation, "assistant", "", status="pending"
    )
    run = repository.create_run(session, conversation, user_message, assistant_message)
    repository.update_run_status(run, "running")
    session.commit()

    stored = repository.get_conversation(
        session, conversation.id, test_workspace.id, test_user.id
    )
    assert stored is not None
    assert stored.mode == "fast"
    assert repository.get_messages(session, conversation.id)[1].status == "pending"
    assert run.engine == "fast"


def test_permission_response_is_durable_and_single_use(session, test_user, test_workspace):
    conversation = repository.create_conversation(
        session, test_workspace.id, test_user.id, "expert", "权限测试"
    )
    user_message = repository.create_message(session, conversation, "user", "运行检查")
    assistant_message = repository.create_message(
        session, conversation, "assistant", "", status="pending"
    )
    run = repository.create_run(session, conversation, user_message, assistant_message)
    repository.update_run_status(run, "waiting_permission")
    run.pending_permission_id = "perm-1"
    session.commit()

    assert repository.set_permission_response(session, run.id, "perm-1", "once")
    session.commit()
    assert repository.get_permission_response(session, run.id, "perm-1") == "once"
    assert not repository.set_permission_response(session, run.id, "perm-1", "reject")


def test_fast_run_persists_stream_and_terminal_state(session, test_user, test_workspace):
    conversation = repository.create_conversation(
        session, test_workspace.id, test_user.id, "fast", "快速 Run 测试"
    )
    session.commit()
    conversation, run, history = service.prepare_run(
        session,
        test_workspace,
        test_user.id,
        conversation.id,
        "检查资料",
        request_id="req-test-1",
    )

    class FakeFastEngine:
        async def stream(self, **_kwargs):
            yield AssistantEvent("token", {"content": "已找到"})

    async def collect():
        return [
            event
            async for event in service.stream_prepared_run(
                session,
                test_workspace,
                test_user.id,
                conversation,
                run,
                history,
            )
        ]

    with patch("app.features.assistant.service.FastEngine", FakeFastEngine):
        events = asyncio.run(collect())

    assert [event.type for event in events] == ["run_started", "token", "done"]
    session.refresh(run)
    assert run.status == "completed"
    message = session.get(AssistantMessage, run.assistant_message_id)
    assert message is not None
    assert message.status == "completed"
    assert message.content == "已找到"


def test_viewer_unfiltered_list_hides_expert_metadata(session, test_user, test_workspace):
    viewer = User(username="assistant-viewer", role="common")
    viewer.set_password("viewer-password")
    session.add(viewer)
    session.flush()
    session.add(
        WorkspaceMember(
            workspace_id=test_workspace.id,
            user_id=viewer.id,
            role="viewer",
        )
    )
    repository.create_conversation(session, test_workspace.id, viewer.id, "fast", "资料")
    repository.create_conversation(session, test_workspace.id, viewer.id, "expert", "专家")
    session.commit()

    visible = service.list_user_conversations(session, test_workspace, viewer.id)
    assert [conversation.mode for conversation in visible] == ["fast"]


def test_fast_engine_adapts_legacy_sse_to_internal_events():
    async def fake_events(*_args, **_kwargs):
        yield 'data: {"type":"keywords","content":"退款"}' + chr(10) + chr(10)
        yield 'data: {"type":"token","content":"答案"}' + chr(10) + chr(10)

    async def collect():
        return [
            event
            async for event in FastEngine().stream(
                run_id=7,
                user_id=1,
                workspace_id=2,
                query="退款",
                history=[],
            )
        ]

    with patch("app.features.assistant.engines.fast_engine.generate_chat_events", fake_events):
        events = asyncio.run(collect())
    assert [event.type for event in events] == ["keywords", "token"]
    assert events[1].payload["content"] == "答案"


def test_opencode_event_mapping_filters_other_sessions():
    state: dict[str, str] = {}
    event = {
        "type": "message.part.updated",
        "properties": {
            "sessionID": "other",
            "part": {"id": "p1", "type": "text", "text": "secret"},
        },
    }
    assert map_opencode_event(event, "ses_current", state) == []


def test_opencode_event_mapping_emits_token_tool_and_permission():
    state: dict[str, str] = {}
    token = map_opencode_event(
        {
            "type": "message.part.updated",
            "properties": {"sessionID": "ses_1", "part": {"id": "p1", "type": "text", "text": "hello"}},
        },
        "ses_1",
        state,
    )
    tool = map_opencode_event(
        {
            "type": "message.part.updated",
            "properties": {"sessionID": "ses_1", "part": {"id": "p2", "type": "tool", "name": "rg", "status": "running"}},
        },
        "ses_1",
        state,
    )
    permission = map_opencode_event(
        {
            "type": "permission.asked",
            "properties": {"sessionID": "ses_1", "permission": {"id": "perm_1", "title": "Run command", "command": "pytest"}},
        },
        "ses_1",
        state,
    )
    assert token[0].type == "token"
    assert tool[0].type == "tool_started"
    assert permission[0].type == "permission_required"
    assert permission[0].payload["permission_id"] == "perm_1"


def test_runtime_basic_auth_is_deterministic_and_scoped():
    first = server_password(1, 10, 20)
    assert first == server_password(1, 10, 20)
    assert first != server_password(2, 10, 20)
    assert first != server_password(1, 11, 20)


async def test_runtime_token_cannot_call_rest_user_dependency(session, test_user):
    from app.api.dependencies import get_current_user
    from fastapi import HTTPException

    token = jwt.encode(
        {"sub": str(test_user.id), "type": "mcp_runtime"},
        SECRET_KEY,
        algorithm="HS256",
    )
    try:
        await get_current_user(credentials=None, token=token, session=session)
    except HTTPException as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("runtime token was accepted by the ordinary REST dependency")


def test_handoff_creates_separate_expert_conversation(session, test_user, test_workspace):
    fast = repository.create_conversation(session, test_workspace.id, test_user.id, "fast", "查询")
    user_message = repository.create_message(session, fast, "user", "继续分析")
    assistant_message = repository.create_message(
        session,
        fast,
        "assistant",
        "建议检查导入日志",
        metadata={"sources": [{"file_id": 3, "file_name": "import.md", "page_number": 2}]},
    )
    session.commit()
    target = service.handoff_to_expert(
        session, test_workspace, test_user.id, fast.id, assistant_message.id
    )
    assert target.mode == "expert"
    assert target.source_conversation_id == fast.id
    system_message = repository.get_messages(session, target.id)[0]
    assert "import.md" in system_message.content
