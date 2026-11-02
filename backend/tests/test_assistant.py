"""助手持久化、事件映射与安全回归测试."""

import asyncio
from datetime import timedelta
from unittest.mock import MagicMock, patch

import jwt
from fastapi.security import HTTPAuthorizationCredentials
import pytest
from sqlalchemy.orm import sessionmaker

from app.exceptions import BusinessRuleError, ConflictError, ResourceNotFoundError
from app.features.assistant import repository, service
from app.features.assistant.clients.opencode_auth import server_password
from app.features.assistant.clients.opencode_events import map_opencode_event
from app.features.assistant.engines.base import EngineRequest
from app.features.assistant.engines.fast_engine import FastEngine
from app.features.assistant.engines.expert_engine import _text_from_messages
from app.features.assistant.event_protocol import AssistantEvent
from app.features.assistant.router import (
    delete_assistant_conversation,
    get_active_assistant_run_for_conversation,
    list_assistant_conversations,
    update_assistant_conversation,
)
from app.features.assistant.schemas import AssistantConversationUpdate
from app.infra.extensions import SECRET_KEY
from app.infra.datetime_utils import beijing_now
from app.models.assistant import AssistantMessage
from app.models.user import User
from app.models.workspace import WorkspaceMember


def test_assistant_repository_persists_mode_specific_records(session, test_user, test_workspace):
    conversation = repository.create_conversation(
        session,
        repository.CreateConversationParams(
            workspace_id=test_workspace.id,
            user_id=test_user.id,
            mode="fast",
            title="资料查询",
        ),
    )
    user_message = repository.create_message(
        session,
        conversation,
        repository.CreateMessageParams(role="user", content="查找退款规则"),
    )
    assistant_message = repository.create_message(
        session,
        conversation,
        repository.CreateMessageParams(
            role="assistant",
            content="",
            status="pending",
        ),
    )
    run = repository.create_run(session, conversation, user_message, assistant_message)
    repository.update_run_status(session, run, "running")
    session.commit()

    stored = repository.get_conversation(
        session, conversation.id, test_workspace.id, test_user.id
    )
    assert stored is not None
    assert stored.mode == "fast"
    assert repository.get_messages(session, conversation.id)[1].status == "pending"
    assert run.engine == "fast"


def test_user_can_rename_archive_restore_and_delete_own_conversation(
    session, test_user, test_workspace
):
    conversation = service.create_conversation(
        session, test_workspace, test_user.id, "fast", "初始标题"
    )

    renamed = service.update_conversation(
        session,
        service.UpdateConversationParams(
            workspace=test_workspace,
            user_id=test_user.id,
            conversation_id=conversation.id,
            title="  项目资料  ",
        ),
    )
    assert renamed.title == "项目资料"

    archived = service.update_conversation(
        session,
        service.UpdateConversationParams(
            workspace=test_workspace,
            user_id=test_user.id,
            conversation_id=conversation.id,
            status="archived",
        ),
    )
    assert archived.status == "archived"
    assert conversation not in service.list_user_conversations(
        session, test_workspace, test_user.id, "fast"
    )
    assert conversation in service.list_user_conversations(
        session, test_workspace, test_user.id, "fast", include_archived=True
    )

    restored = service.update_conversation(
        session,
        service.UpdateConversationParams(
            workspace=test_workspace,
            user_id=test_user.id,
            conversation_id=conversation.id,
            status="active",
        ),
    )
    assert restored.status == "active"
    assert conversation in service.list_user_conversations(
        session, test_workspace, test_user.id, "fast"
    )

    service.delete_conversation(session, test_workspace, test_user.id, conversation.id)
    assert repository.get_conversation(
        session, conversation.id, test_workspace.id, test_user.id
    ) is None


def test_conversation_management_validates_state_and_blocks_active_run(
    session, test_user, test_workspace
):
    conversation = service.create_conversation(
        session, test_workspace, test_user.id, "fast", "运行中的会话"
    )
    _, run, _ = service.prepare_run(
        session,
        service.PrepareRunParams(
            workspace=test_workspace,
            user_id=test_user.id,
            conversation_id=conversation.id,
            query="保持运行",
            request_id="req-management-active",
        ),
    )

    with pytest.raises(ConflictError, match="先停止任务"):
        service.update_conversation(
            session,
            service.UpdateConversationParams(
                workspace=test_workspace,
                user_id=test_user.id,
                conversation_id=conversation.id,
                status="archived",
            ),
        )
    with pytest.raises(ConflictError, match="先停止任务"):
        service.delete_conversation(session, test_workspace, test_user.id, conversation.id)
    with pytest.raises(BusinessRuleError, match="状态无效"):
        service.update_conversation(
            session,
            service.UpdateConversationParams(
                workspace=test_workspace,
                user_id=test_user.id,
                conversation_id=conversation.id,
                status="deleted",
            ),
        )

    repository.update_run_status(session, run, "cancelled")
    session.commit()
    service.update_conversation(
        session,
        service.UpdateConversationParams(
            workspace=test_workspace,
            user_id=test_user.id,
            conversation_id=conversation.id,
            status="archived",
        ),
    )
    with pytest.raises(ConflictError, match="先恢复后再发送"):
        service.prepare_run(
            session,
            service.PrepareRunParams(
                workspace=test_workspace,
                user_id=test_user.id,
                conversation_id=conversation.id,
                query="归档会话不能发送",
            ),
        )
    service.delete_conversation(session, test_workspace, test_user.id, conversation.id)


def test_conversation_management_does_not_cross_user_boundary(
    session, test_user, test_workspace
):
    other_user = User(username="assistant-owner-2", role="common")
    other_user.set_password("owner-password")
    session.add(other_user)
    session.flush()
    session.add(
        WorkspaceMember(
            workspace_id=test_workspace.id,
            user_id=other_user.id,
            role="editor",
        )
    )
    session.flush()
    conversation = service.create_conversation(
        session, test_workspace, other_user.id, "fast", "别人的会话"
    )
    session.commit()

    with pytest.raises(ResourceNotFoundError):
        service.update_conversation(
            session,
            service.UpdateConversationParams(
                workspace=test_workspace,
                user_id=test_user.id,
                conversation_id=conversation.id,
                title="越权修改",
            ),
        )
    with pytest.raises(ResourceNotFoundError):
        service.delete_conversation(session, test_workspace, test_user.id, conversation.id)


def test_conversation_management_handlers_cover_list_update_active_run_and_delete(
    session, test_user, test_workspace
):
    conversation = service.create_conversation(
        session, test_workspace, test_user.id, "fast", "HTTP 会话"
    )
    session_factory = sessionmaker(
        bind=session.get_bind(),
        join_transaction_mode="create_savepoint",
    )

    updated = (
        update_assistant_conversation(
            conversation_id=conversation.id,
            payload=AssistantConversationUpdate(title="接口重命名"),
            current_user=test_user,
            workspace=test_workspace,
            session_factory=session_factory,
        )
    )
    assert updated["data"]["title"] == "接口重命名"

    active_run = (
        get_active_assistant_run_for_conversation(
            conversation_id=conversation.id,
            current_user=test_user,
            workspace=test_workspace,
            session_factory=session_factory,
        )
    )
    assert active_run == {"success":True,"data":None}

    (
        update_assistant_conversation(
            conversation_id=conversation.id,
            payload=AssistantConversationUpdate(status="archived"),
            current_user=test_user,
            workspace=test_workspace,
            session_factory=session_factory,
        )
    )
    active_only = (
        list_assistant_conversations(
            mode="fast",
            include_archived=False,
            current_user=test_user,
            workspace=test_workspace,
            session_factory=session_factory,
        )
    )
    all_sessions = (
        list_assistant_conversations(
            mode="fast",
            include_archived=True,
            current_user=test_user,
            workspace=test_workspace,
            session_factory=session_factory,
        )
    )
    assert conversation.id not in {item["id"] for item in active_only["data"]["conversations"]}
    assert conversation.id in {item["id"] for item in all_sessions["data"]["conversations"]}

    assert (
        delete_assistant_conversation(
            conversation_id=conversation.id,
            current_user=test_user,
            workspace=test_workspace,
            session_factory=session_factory,
        )
    ) is None
    assert repository.get_conversation(
        session, conversation.id, test_workspace.id, test_user.id
    ) is None


def test_permission_response_is_durable_and_single_use(session, test_user, test_workspace):
    conversation = repository.create_conversation(
        session,
        repository.CreateConversationParams(
            workspace_id=test_workspace.id,
            user_id=test_user.id,
            mode="expert",
            title="权限测试",
        ),
    )
    user_message = repository.create_message(
        session,
        conversation,
        repository.CreateMessageParams(role="user", content="运行检查"),
    )
    assistant_message = repository.create_message(
        session,
        conversation,
        repository.CreateMessageParams(
            role="assistant",
            content="",
            status="pending",
        ),
    )
    run = repository.create_run(session, conversation, user_message, assistant_message)
    repository.register_permission(
        session,
        run,
        {"permission_id": "perm-1", "title": "Run check"},
    )
    session.commit()

    assert repository.answer_permission(session, run, "perm-1", "once")
    session.commit()
    permission = repository.permission_for_run(session, run.id, "perm-1")
    assert permission is not None
    assert permission.state == "answered" and permission.response == "once"
    assert not repository.answer_permission(session, run, "perm-1", "reject")


def test_permission_registration_is_idempotent_and_allows_one_open_request(
    session,
    test_user,
    test_workspace,
):
    conversation = repository.create_conversation(
        session,
        repository.CreateConversationParams(
            workspace_id=test_workspace.id,
            user_id=test_user.id,
            mode="expert",
            title="permission state",
        ),
    )
    user = repository.create_message(
        session,
        conversation,
        repository.CreateMessageParams(role="user", content="run"),
    )
    assistant = repository.create_message(
        session,
        conversation,
        repository.CreateMessageParams(role="assistant", status="pending"),
    )
    run = repository.create_run(session, conversation, user, assistant)
    first = repository.register_permission(
        session,
        run,
        {"permission_id": "p1", "title": "First request"},
    )
    replay = repository.register_permission(
        session,
        run,
        {"permission_id": "p1", "title": "replacement details"},
    )
    assert replay.details == first.details
    with pytest.raises(ConflictError, match="Another permission request"):
        repository.register_permission(
            session,
            run,
            {"permission_id": "p2", "title": "Second request"},
        )

    assert repository.answer_permission(session, run, "p1", "once")
    with pytest.raises(ConflictError, match="Another permission request"):
        repository.register_permission(
            session,
            run,
            {"permission_id": "p2", "title": "Second request"},
        )
    first = repository.permission_for_run(session, run.id, "p1")
    assert first is not None
    repository.mark_permission_dispatched(session, run, first)
    next_permission = repository.register_permission(
        session,
        run,
        {"permission_id": "p2", "title": "Second request"},
    )
    assert first.state == "dispatched" and first.response == "once"
    assert next_permission.state == "pending"
    assert run.pending_permission_id == "p2"


@pytest.mark.parametrize("engine_cancelled", [False, True])
def test_fast_run_persists_stream_and_terminal_state(session, test_user, test_workspace, engine_cancelled):
    conversation = repository.create_conversation(
        session,
        repository.CreateConversationParams(
            workspace_id=test_workspace.id,
            user_id=test_user.id,
            mode="fast",
            title="快速 Run 测试",
        ),
    )
    session.commit()
    conversation, run, history = service.prepare_run(
        session,
        service.PrepareRunParams(
            workspace=test_workspace,
            user_id=test_user.id,
            conversation_id=conversation.id,
            query="检查资料",
            request_id="req-test-1",
        ),
    )
    workspace_id = int(test_workspace.id)
    user_id = int(test_user.id)
    conversation_id = int(conversation.id)
    run_id = int(run.id)

    class FakeFastEngine:
        async def stream(self, request):
            assert isinstance(request, EngineRequest)
            assert request.run_id == run_id
            assert request.user_id == user_id
            assert request.workspace_id == workspace_id
            assert request.query == "检查资料"
            assert request.history == history
            assert request.controller is not None
            yield AssistantEvent("token", {"content": "已找到"})
            if engine_cancelled:
                yield AssistantEvent("cancelled", {"status": "cancelled"})

    async def collect():
        params = service.StreamRunParams(
            session_factory=sessionmaker(
                bind=session.get_bind(),
                join_transaction_mode="create_savepoint",
            ),
            workspace_id=workspace_id,
            user_id=user_id,
            conversation_id=conversation_id,
            run_id=run_id,
            history=history,
        )
        return [event async for event in service.stream_run(params)]

    with patch(
        "app.features.assistant.engines.fast_engine.FastEngine",
        FakeFastEngine,
    ):
        events = asyncio.run(collect())

    terminal = "cancelled" if engine_cancelled else "completed"
    assert [event.type for event in events] == ["run_started", "token", "cancelled" if engine_cancelled else "done"]
    session.refresh(run)
    assert run.status == terminal
    message = session.get(AssistantMessage, run.assistant_message_id)
    assert message is not None
    assert message.status == terminal
    assert message.content == "已找到"


def test_prepare_run_recovers_abandoned_stream(session, test_user, test_workspace):
    conversation = repository.create_conversation(
        session,
        repository.CreateConversationParams(
            workspace_id=test_workspace.id,
            user_id=test_user.id,
            mode="fast",
            title="恢复测试",
        ),
    )
    session.commit()
    conversation, abandoned_run, _ = service.prepare_run(
        session,
        service.PrepareRunParams(
            workspace=test_workspace,
            user_id=test_user.id,
            conversation_id=conversation.id,
            query="第一次请求",
            request_id="req-abandoned-1",
        ),
    )
    abandoned_run.started_at = beijing_now() - timedelta(minutes=5)
    session.commit()

    _, replacement_run, _ = service.prepare_run(
        session,
        service.PrepareRunParams(
            workspace=test_workspace,
            user_id=test_user.id,
            conversation_id=conversation.id,
            query="重新发送",
            request_id="req-abandoned-2",
        ),
    )

    session.refresh(abandoned_run)
    assert abandoned_run.status == "failed"
    assert abandoned_run.error_code == "stream_abandoned"
    assert replacement_run.id != abandoned_run.id
    abandoned_message = session.get(AssistantMessage, abandoned_run.assistant_message_id)
    assert abandoned_message is not None
    assert abandoned_message.status == "failed"


def test_prepare_run_recovers_abandoned_expert_run_from_another_conversation(
    session, test_user, test_workspace
):
    old_conversation = repository.create_conversation(
        session,
        repository.CreateConversationParams(
            workspace_id=test_workspace.id,
            user_id=test_user.id,
            mode="expert",
            title="旧专家会话",
        ),
    )
    old_user_message = repository.create_message(
        session,
        old_conversation,
        repository.CreateMessageParams(role="user", content="旧请求"),
    )
    old_assistant_message = repository.create_message(
        session,
        old_conversation,
        repository.CreateMessageParams(
            role="assistant",
            content="",
            status="streaming",
        ),
    )
    abandoned_run = repository.create_run(
        session,
        old_conversation,
        old_user_message,
        old_assistant_message,
    )
    repository.update_run_status(session, abandoned_run, "running")
    abandoned_run.started_at = beijing_now() - timedelta(minutes=5)

    new_conversation = repository.create_conversation(
        session,
        repository.CreateConversationParams(
            workspace_id=test_workspace.id,
            user_id=test_user.id,
            mode="expert",
            title="新专家会话",
        ),
    )
    session.commit()

    _, replacement_run, _ = service.prepare_run(
        session,
        service.PrepareRunParams(
            workspace=test_workspace,
            user_id=test_user.id,
            conversation_id=new_conversation.id,
            query="新请求",
            request_id="req-after-abandoned-expert",
        ),
    )

    session.refresh(abandoned_run)
    assert abandoned_run.status == "failed"
    assert abandoned_run.error_code == "stream_abandoned"
    assert replacement_run.conversation_id == new_conversation.id


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
    repository.create_conversation(
        session,
        repository.CreateConversationParams(
            workspace_id=test_workspace.id,
            user_id=viewer.id,
            mode="fast",
            title="资料",
        ),
    )
    repository.create_conversation(
        session,
        repository.CreateConversationParams(
            workspace_id=test_workspace.id,
            user_id=viewer.id,
            mode="expert",
            title="专家",
        ),
    )
    session.commit()

    visible = service.list_user_conversations(session, test_workspace, viewer.id)
    assert [conversation.mode for conversation in visible] == ["fast"]




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
            "properties": {
                "sessionID": "ses_1",
                "permission": {
                    "id": "perm_1",
                    "title": "Run command",
                    "command": {"name": "pytest", "input": {"args": ["tests"]}},
                },
            },
        },
        "ses_1",
        state,
    )
    assert token[0].type == "token"
    assert tool[0].type == "tool_started"
    assert permission[0].type == "permission_required"
    assert permission[0].payload["permission_id"] == "perm_1"
    assert permission[0].payload["tool"] == 'pytest: {"args":["tests"]}'


def test_opencode_event_mapping_hides_user_prompt_and_reasoning():
    state = {"__user_query": "安装前端 Skill"}
    map_opencode_event(
        {
            "type": "message.updated",
            "properties": {"info": {"id": "msg_user", "role": "user"}},
        },
        "ses_1",
        state,
    )
    user_part = map_opencode_event(
        {
            "type": "message.part.updated",
            "properties": {
                "part": {
                    "id": "part_user",
                    "messageID": "msg_user",
                    "type": "text",
                    "text": "安装前端 Skill",
                }
            },
        },
        "ses_1",
        state,
    )
    reasoning = map_opencode_event(
        {
            "type": "message.part.updated",
            "properties": {
                "part": {
                    "id": "part_reasoning",
                    "messageID": "msg_assistant",
                    "type": "reasoning",
                    "text": "**Planning the answer**",
                }
            },
        },
        "ses_1",
        state,
    )
    assistant = map_opencode_event(
        {
            "type": "message.updated",
            "properties": {"info": {"id": "msg_assistant", "role": "assistant"}},
        },
        "ses_1",
        state,
    )
    assistant.extend(
        map_opencode_event(
            {
                "type": "message.part.updated",
                "properties": {
                    "part": {
                        "id": "part_answer",
                        "messageID": "msg_assistant",
                        "type": "text",
                        "text": "回答内容",
                    }
                },
            },
            "ses_1",
            state,
        )
    )

    assert user_part == []
    assert reasoning[0].type == "title"
    assert reasoning[0].payload["content"] == "Planning the answer"
    assert [event.payload["content"] for event in assistant if event.type == "token"] == ["回答内容"]


def test_opencode_message_fallback_separates_title_from_answer():
    text, provider_id, usage, title = _text_from_messages(
        [
            {
                "info": {"id": "msg_user", "role": "user"},
                "parts": [{"type": "text", "text": "询问内容"}],
            },
            {
                "info": {
                    "id": "msg_assistant",
                    "role": "assistant",
                    "tokens": {"output": 12},
                },
                "parts": [
                    {"type": "reasoning", "text": "**内部规划**"},
                    {"type": "text", "text": "最终回答"},
                ],
            },
        ]
    )

    assert text == "最终回答"
    assert provider_id == "msg_assistant"
    assert usage == {"output": 12}
    assert title == "内部规划"


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
        await get_current_user(credentials=HTTPAuthorizationCredentials(scheme="Bearer", credentials=token), session_factory=MagicMock())
    except HTTPException as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("runtime token was accepted by the ordinary REST dependency")


def test_handoff_creates_separate_expert_conversation(session, test_user, test_workspace):
    fast = repository.create_conversation(
        session,
        repository.CreateConversationParams(
            workspace_id=test_workspace.id,
            user_id=test_user.id,
            mode="fast",
            title="查询",
        ),
    )
    user_message = repository.create_message(
        session,
        fast,
        repository.CreateMessageParams(role="user", content="继续分析"),
    )
    assistant_message = repository.create_message(
        session,
        fast,
        repository.CreateMessageParams(
            role="assistant",
            content="建议检查导入日志",
            metadata={
                "sources": [
                    {
                        "file_id": 3,
                        "file_name": "import.md",
                        "page_number": 2,
                    }
                ]
            },
        ),
    )
    session.commit()
    target = service.handoff_to_expert(
        session, test_workspace, test_user.id, fast.id, assistant_message.id
    )
    assert target.mode == "expert"
    assert target.source_conversation_id == fast.id
    system_message = repository.get_messages(session, target.id)[0]
    assert "import.md" in system_message.content


def test_pending_permission_is_durable_bounded_and_response_gated(
    session,
    test_user,
    test_workspace,
):
    from sqlalchemy.orm import Session

    conversation = repository.create_conversation(
        session,
        repository.CreateConversationParams(
            workspace_id=test_workspace.id,
            user_id=test_user.id,
            mode="expert",
            title="task",
        ),
    )
    user = repository.create_message(
        session,
        conversation,
        repository.CreateMessageParams(role="user", content="patch"),
    )
    assistant = repository.create_message(
        session,
        conversation,
        repository.CreateMessageParams(
            role="assistant",
            content="",
            status="pending",
        ),
    )
    run = repository.create_run(session, conversation, user, assistant)
    repository.register_permission(
        session,
        run,
        {
            "permission_id": "p1",
            "title": "Patch a.txt",
            "tool": "apply_patch a.txt",
            "provider_secret": "never-persist",
        },
    )
    session.commit()
    with Session(bind=session.get_bind()) as restored:
        recovered = restored.get(type(run), run.id)
        assert recovered.to_dict()["pending_permission"] == {
            "permission_id": "p1",
            "title": "Patch a.txt",
            "tool": "apply_patch a.txt",
        }
        stored = repository.permission_for_run(restored, run.id, "p1")
        assert stored is not None
        assert stored.details == recovered.to_dict()["pending_permission"]
        assert "provider_secret" not in stored.details_json
        assert repository.answer_permission(restored, recovered, "p1", "once")
        restored.commit()
        assert recovered.to_dict()["pending_permission"] is None
        assert not repository.answer_permission(
            restored,
            recovered,
            "p1",
            "reject",
        )
        replay = repository.register_permission(
            restored,
            recovered,
            {"permission_id": "p1", "title": "replacement details"},
        )
        assert replay.state == "answered"
        assert replay.response == "once"
        assert replay.details == stored.details


@pytest.mark.parametrize(
    "state",
    ["running", "completed", "failed", "cancelled", "cancel_requested", "answered"],
)
def test_pending_permission_is_not_recovered_outside_matching_wait(
    session,
    test_user,
    test_workspace,
    state,
):
    conversation = repository.create_conversation(
        session,
        repository.CreateConversationParams(
            workspace_id=test_workspace.id,
            user_id=test_user.id,
            mode="expert",
            title="task",
        ),
    )
    user = repository.create_message(
        session,
        conversation,
        repository.CreateMessageParams(role="user", content="patch"),
    )
    assistant = repository.create_message(
        session,
        conversation,
        repository.CreateMessageParams(
            role="assistant",
            content="",
            status="pending",
        ),
    )
    run = repository.create_run(session, conversation, user, assistant)
    repository.register_permission(
        session,
        run,
        {"permission_id": "p1", "title": "Patch a.txt"},
    )
    if state == "cancel_requested":
        run.cancel_requested = True
    elif state == "answered":
        assert repository.answer_permission(session, run, "p1", "once")
    else:
        repository.update_run_status(session, run, state)
    assert run.to_dict()["pending_permission"] is None
    if state in {"completed", "failed", "cancelled"}:
        assert run.pending_permission_id is None
        permission = repository.permission_for_run(session, run.id, "p1")
        assert permission is not None and permission.state == "cancelled"
    elif state == "answered":
        assert run.pending_permission_id == "p1"


def test_permission_wait_survives_heartbeat_and_status_events(session, test_user, test_workspace):
    conversation = repository.create_conversation(
        session,
        repository.CreateConversationParams(
            workspace_id=test_workspace.id,
            user_id=test_user.id,
            mode='fast',
            title='heartbeat',
        ),
    )
    session.commit()
    conversation, run, history = service.prepare_run(
        session,
        service.PrepareRunParams(
            workspace=test_workspace,
            user_id=test_user.id,
            conversation_id=conversation.id,
            query='check',
        ),
    )
    repository.register_permission(
        session,
        run,
        {"permission_id": "p1", "title": "Patch a.txt"},
    )
    session.commit()
    workspace_id = int(test_workspace.id)
    user_id = int(test_user.id)
    conversation_id = int(conversation.id)
    run_id = int(run.id)

    class WaitingEngine:
        async def stream(self, request):
            yield AssistantEvent('permission_required', {'permission_id': 'p1', 'title': 'Patch a.txt'})
            yield AssistantEvent('heartbeat', {})
            yield AssistantEvent('status', {'content': 'still waiting'})

    async def collect():
        params = service.StreamRunParams(
            session_factory=sessionmaker(
                bind=session.get_bind(),
                join_transaction_mode="create_savepoint",
            ),
            workspace_id=workspace_id,
            user_id=user_id,
            conversation_id=conversation_id,
            run_id=run_id,
            history=history,
        )
        events = []
        async for event in service.stream_run(params):
            events.append(event)
            if event.type in {'permission_required', 'heartbeat', 'status'}:
                session.refresh(run)
                assert run.status == 'waiting_permission'
                assert run.to_dict()['pending_permission']['permission_id'] == 'p1'
        assert [event.type for event in events] == [
            'run_started',
            'permission_required',
            'heartbeat',
            'status',
            'done',
        ]
        session.refresh(run)
        assert run.status == 'completed'
        assert run.to_dict()['pending_permission'] is None

    with patch(
        'app.features.assistant.engines.fast_engine.FastEngine',
        WaitingEngine,
    ):
        asyncio.run(collect())


@pytest.mark.parametrize('heartbeat', [None, False, 'now', float('nan'), 10**20])
def test_invalid_heartbeat_does_not_pin_an_abandoned_run(session, test_user, test_workspace, heartbeat):
    conversation = repository.create_conversation(
        session,
        repository.CreateConversationParams(
            workspace_id=test_workspace.id,
            user_id=test_user.id,
            mode='expert',
            title='task',
        ),
    )
    user = repository.create_message(
        session,
        conversation,
        repository.CreateMessageParams(role='user', content='patch'),
    )
    assistant = repository.create_message(
        session,
        conversation,
        repository.CreateMessageParams(
            role='assistant',
            content='',
            status='pending',
        ),
    )
    run = repository.create_run(session, conversation, user, assistant)
    repository.update_run_status(session, run, "running")
    run.started_at = beijing_now() - timedelta(minutes=5)
    run.metadata_dict = {'worker_heartbeat_unix': heartbeat}
    session.commit()
    assert service.recover_abandoned_run(session, run)
    assert run.status == 'failed'


def test_cancel_return_closes_engine_producer_before_stream_returns(session, test_user, test_workspace):
    conversation = repository.create_conversation(
        session,
        repository.CreateConversationParams(
            workspace_id=test_workspace.id,
            user_id=test_user.id,
            mode='fast',
            title='cancel',
        ),
    )
    session.commit()
    conversation, run, history = service.prepare_run(
        session,
        service.PrepareRunParams(
            workspace=test_workspace,
            user_id=test_user.id,
            conversation_id=conversation.id,
            query='check',
        ),
    )
    workspace_id = int(test_workspace.id)
    user_id = int(test_user.id)
    conversation_id = int(conversation.id)
    run_id = int(run.id)
    closed = asyncio.Event()

    class WaitingEngine:
        async def stream(self, request):
            try:
                request.controller.cancel_event.set()
                yield AssistantEvent('status', {'content': 'wait'})
                await asyncio.Event().wait()
            finally:
                closed.set()

    async def collect():
        params = service.StreamRunParams(
            session_factory=sessionmaker(
                bind=session.get_bind(),
                join_transaction_mode="create_savepoint",
            ),
            workspace_id=workspace_id,
            user_id=user_id,
            conversation_id=conversation_id,
            run_id=run_id,
            history=history,
        )
        events = [event async for event in service.stream_run(params)]
        assert events[-1].type == 'cancelled'
        assert closed.is_set()
        assert service.run_registry.get(run_id) is None

    with patch(
        'app.features.assistant.engines.fast_engine.FastEngine',
        WaitingEngine,
    ):
        asyncio.run(collect())


def test_current_permission_contract_displays_command_instead_of_tool_pointer():
    properties = {'id': 'p1', 'sessionID': 's1', 'permission': 'bash', 'patterns': ["printf 'marker'"],
                  'metadata': {'command': "printf 'marker'", 'description': 'Print'},
                  'tool': {'messageID': 'message-pointer', 'callID': 'call-pointer'}}
    event = map_opencode_event({'type': 'permission.asked', 'properties': properties}, 's1', {})[0]
    assert event.payload['title'] == '请求权限:bash'
    assert event.payload['tool'] == "bash: printf 'marker'"
    properties['metadata'] = {}
    assert "printf 'marker'" in map_opencode_event({'type': 'permission.asked', 'properties': properties}, 's1', {})[0].payload['tool']
    properties.pop('sessionID')
    assert map_opencode_event({'type': 'permission.asked', 'properties': properties}, 's1', {}) == []


def test_permission_display_marks_truncation_explicitly():
    event = map_opencode_event({'type': 'permission.asked', 'properties': {'id': 'p1', 'sessionID': 's1', 'permission': 'bash', 'metadata': {'command': 'a' * 5000}}}, 's1', {})[0]
    assert len(event.payload['tool']) == 4000
    assert event.payload['tool'].endswith('(内容已截断)')


def test_opencode_permission_transport_is_strictly_one_shot():
    from unittest.mock import AsyncMock
    from app.features.assistant.clients.opencode_client import OpenCodeClient
    async def exercise():
        async with OpenCodeClient('http://runtime', 'user', 'password') as client:
            with patch.object(client, '_json', AsyncMock(return_value=True)) as request:
                assert await client.respond_permission('s1', 'p1', 'once')
                request.assert_awaited_once_with('POST', '/session/s1/permissions/p1', json={'response': 'once'})
                for response, remember in [('always', False), ('once', True)]:
                    with pytest.raises(ValueError, match='one-shot'):
                        await client.respond_permission(
                            's1', 'p1', response, remember=remember)
                assert request.await_count == 1
    asyncio.run(exercise())
