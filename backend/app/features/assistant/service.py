"""Assistant orchestration: ownership, persistence, cancellation, and SSE."""

import asyncio
import os
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import Any

from sqlalchemy.orm import Session

from app.exceptions import (
    BusinessRuleError,
    ConflictError,
    PermissionDeniedError,
    ResourceNotFoundError,
)
from app.features.assistant import repository
from app.features.assistant.engines.fast_engine import FastEngine
from app.features.assistant.event_protocol import AssistantEvent, heartbeat_event
from app.features.workspace.permissions import assert_can_write, assert_member
from app.infra.datetime_utils import beijing_now
from app.models.assistant import AssistantConversation, AssistantMessage, AssistantRun
from app.models.workspace import Workspace


class RunController:
    """In-process control plane for a currently streaming run.

    Database status is the durable source of truth.  These events/callbacks
    are only latency optimizations for cancellation and permission responses
    while the HTTP stream is alive.
    """

    def __init__(self) -> None:
        self.cancel_event = asyncio.Event()
        self.abort_callback: Callable[[], Awaitable[Any]] | None = None
        self._permission_futures: dict[str, asyncio.Future[str]] = {}

    def set_abort_callback(self, callback: Callable[[], Awaitable[Any]]) -> None:
        self.abort_callback = callback

    async def request_cancel(self) -> None:
        self.cancel_event.set()
        if self.abort_callback:
            try:
                await self.abort_callback()
            except Exception:
                # The run is still marked cancelled in the database.  The
                # provider abort is best effort during client disconnects.
                pass

    def wait_for_permission(self, permission_id: str) -> asyncio.Future[str]:
        future = self._permission_futures.get(permission_id)
        if future is None:
            future = asyncio.get_running_loop().create_future()
            self._permission_futures[permission_id] = future
        return future

    def resolve_permission(self, permission_id: str, response: str) -> bool:
        future = self._permission_futures.get(permission_id)
        if future is None or future.done():
            return False
        future.set_result(response)
        return True


class RunRegistry:
    def __init__(self) -> None:
        self._controllers: dict[int, RunController] = {}

    def register(self, run_id: int) -> RunController:
        controller = RunController()
        self._controllers[run_id] = controller
        return controller

    def get(self, run_id: int) -> RunController | None:
        return self._controllers.get(run_id)

    def unregister(self, run_id: int) -> None:
        self._controllers.pop(run_id, None)

    async def request_cancel(self, run_id: int) -> bool:
        controller = self.get(run_id)
        if controller is None:
            return False
        await controller.request_cancel()
        return True

    def resolve_permission(self, run_id: int, permission_id: str, response: str) -> bool:
        controller = self.get(run_id)
        return bool(controller and controller.resolve_permission(permission_id, response))


run_registry = RunRegistry()


def _recover_abandoned_run(session: Session, run: AssistantRun) -> bool:
    """Release a run left active after its SSE worker has disappeared.

    A browser disconnect normally cancels the generator and records a terminal
    status.  A process restart or an exception before the generator starts can
    leave a durable ``running`` row behind, which would otherwise permanently
    block its conversation.  Keep a short grace period so a duplicate submit
    still receives the normal conflict response.
    """

    if run_registry.get(int(run.id)) is not None:
        return False
    started_at = run.started_at or run.created_at
    if started_at is None:
        return False
    try:
        grace_seconds = max(int(os.getenv("ASSISTANT_ABANDONED_RUN_GRACE_SECONDS", "45")), 0)
    except ValueError:
        grace_seconds = 45
    if (beijing_now() - started_at).total_seconds() < grace_seconds:
        return False

    assistant_message = session.get(AssistantMessage, run.assistant_message_id)
    if assistant_message is not None and assistant_message.status in {"pending", "streaming"}:
        assistant_message.status = "failed"
    run.pending_permission_id = None
    run.permission_response = None
    repository.update_run_status(
        run,
        "failed",
        error_code="stream_abandoned",
        error_message="助手请求在服务重启或连接中断后未完成，请重新发送。",
    )
    session.commit()
    return True


def _conversation_or_404(
    session: Session, conversation_id: int, workspace_id: int, user_id: int
) -> AssistantConversation:
    conversation = repository.get_conversation(session, conversation_id, workspace_id, user_id)
    if conversation is None:
        raise ResourceNotFoundError("Assistant conversation not found")
    return conversation


def create_conversation(
    session: Session,
    workspace: Workspace,
    user_id: int,
    mode: str,
    title: str | None = None,
) -> AssistantConversation:
    assert_member(session, int(workspace.id), int(user_id))
    if mode == "expert":
        assert_can_write(session, int(workspace.id), int(user_id))
    conversation = repository.create_conversation(
        session,
        int(workspace.id),
        int(user_id),
        mode,
        title,
    )
    session.commit()
    return conversation


def list_user_conversations(
    session: Session,
    workspace: Workspace,
    user_id: int,
    mode: str | None = None,
    include_archived: bool = False,
) -> list[AssistantConversation]:
    role = assert_member(session, int(workspace.id), int(user_id))
    if mode == "expert":
        assert_can_write(session, int(workspace.id), int(user_id))
    elif mode is None and role.value == "viewer":
        # Do not leak expert session/runtime metadata through the unfiltered
        # list endpoint.  Editors/admins may intentionally list both modes.
        mode = "fast"
    return repository.list_conversations(
        session,
        int(workspace.id),
        int(user_id),
        mode,
        include_archived=include_archived,
    )


def get_active_run_for_conversation(
    session: Session,
    workspace: Workspace,
    user_id: int,
    conversation_id: int,
) -> AssistantRun | None:
    conversation = get_user_conversation(session, workspace, user_id, conversation_id)
    run = repository.active_run_for_conversation(session, int(conversation.id))
    if run and _recover_abandoned_run(session, run):
        return None
    return run


def get_active_expert_run_for_member(
    session: Session,
    workspace: Workspace,
    user_id: int,
) -> AssistantRun | None:
    assert_can_write(session, int(workspace.id), int(user_id))
    run = repository.active_expert_run_for_member(session, int(workspace.id), int(user_id))
    if run and _recover_abandoned_run(session, run):
        return None
    return run


def get_user_conversation(
    session: Session,
    workspace: Workspace,
    user_id: int,
    conversation_id: int,
) -> AssistantConversation:
    conversation = _conversation_or_404(session, conversation_id, int(workspace.id), int(user_id))
    if conversation.mode == "expert":
        assert_can_write(session, int(workspace.id), int(user_id))
    return conversation


def get_conversation_messages(
    session: Session,
    workspace: Workspace,
    user_id: int,
    conversation_id: int,
) -> list[AssistantMessage]:
    conversation = get_user_conversation(session, workspace, user_id, conversation_id)
    return repository.get_messages(session, int(conversation.id))


def update_conversation(
    session: Session,
    workspace: Workspace,
    user_id: int,
    conversation_id: int,
    *,
    title: str | None = None,
    status: str | None = None,
) -> AssistantConversation:
    get_user_conversation(session, workspace, user_id, conversation_id)
    conversation = repository.lock_conversation(
        session,
        conversation_id,
        int(workspace.id),
        int(user_id),
    )
    if conversation is None:
        raise ResourceNotFoundError("Assistant conversation not found")
    if title is not None:
        normalized_title = title.strip()
        if not normalized_title:
            raise BusinessRuleError("会话标题不能为空")
        conversation.title = normalized_title

    if status is not None:
        if status not in {"active", "archived"}:
            raise BusinessRuleError("会话状态无效")
        active_run = repository.active_run_for_conversation(session, int(conversation.id))
        if active_run and _recover_abandoned_run(session, active_run):
            active_run = None
        if active_run and status == "archived":
            raise ConflictError("当前会话仍有任务运行，请先停止任务")
        conversation.status = status

    session.commit()
    return conversation


def delete_conversation(
    session: Session,
    workspace: Workspace,
    user_id: int,
    conversation_id: int,
) -> None:
    get_user_conversation(session, workspace, user_id, conversation_id)
    conversation = repository.lock_conversation(
        session,
        conversation_id,
        int(workspace.id),
        int(user_id),
    )
    if conversation is None:
        raise ResourceNotFoundError("Assistant conversation not found")
    active_run = repository.active_run_for_conversation(session, int(conversation.id))
    if active_run and _recover_abandoned_run(session, active_run):
        active_run = None
    if active_run:
        raise ConflictError("当前会话仍有任务运行，请先停止任务")
    session.delete(conversation)
    session.commit()


def prepare_run(
    session: Session,
    workspace: Workspace,
    user_id: int,
    conversation_id: int,
    query: str,
    request_id: str | None = None,
) -> tuple[AssistantConversation, AssistantRun, list[dict[str, str]]]:
    """Create the durable message/run records before opening the SSE stream."""

    conversation = get_user_conversation(session, workspace, user_id, conversation_id)
    locked_conversation = repository.lock_conversation(
        session,
        conversation_id,
        int(workspace.id),
        int(user_id),
    )
    if locked_conversation is None:
        raise ResourceNotFoundError("Assistant conversation not found")
    conversation = locked_conversation
    if conversation.status != "active":
        raise ConflictError("会话已归档，请先恢复后再发送")
    if conversation.mode == "expert":
        if repository.lock_workspace_member(session, int(workspace.id), int(user_id)) is None:
            raise PermissionDeniedError("您不是该工作空间的成员")
        # Re-read the role after acquiring the member row lock so a concurrent
        # viewer downgrade cannot race this authorization check.
        assert_can_write(session, int(workspace.id), int(user_id))
        active_member_run = repository.active_expert_run_for_member(
            session, int(workspace.id), int(user_id)
        )
        if active_member_run and _recover_abandoned_run(session, active_member_run):
            active_member_run = None
        if active_member_run:
            raise ConflictError("Expert runtime is already busy")

    active = repository.active_run_for_conversation(session, int(conversation.id))
    if active and _recover_abandoned_run(session, active):
        active = None
    if active:
        raise ConflictError("This assistant conversation already has a running task")

    # The history excludes the new user message and pending assistant shell.
    history = [
        {"role": message.role, "content": message.content}
        for message in repository.get_messages(session, int(conversation.id), limit=20)
        if message.status in {"completed", "failed", "cancelled"}
    ]
    user_message = repository.create_message(
        session, conversation, "user", query.strip(), status="completed"
    )
    assistant_message = repository.create_message(
        session, conversation, "assistant", "", status="pending"
    )
    run = repository.create_run(
        session,
        conversation,
        user_message,
        assistant_message,
        request_id=request_id or str(uuid.uuid4()),
    )
    repository.update_run_status(run, "running")
    assistant_message.status = "streaming"
    session.commit()
    return conversation, run, history


async def _engine_events_with_heartbeats(
    engine_iterator: AsyncIterator[AssistantEvent],
    run_id: int,
    interval_seconds: float = 15.0,
) -> AsyncIterator[AssistantEvent]:
    """Forward provider events while keeping reverse proxies' idle timers warm."""

    queue: asyncio.Queue[tuple[str, AssistantEvent | BaseException | None]] = asyncio.Queue()

    async def produce() -> None:
        try:
            async for event in engine_iterator:
                await queue.put(("event", event))
        except BaseException as exc:  # forwarded to the consumer below
            await queue.put(("error", exc))
        finally:
            await queue.put(("done", None))

    producer = asyncio.create_task(produce())
    try:
        while True:
            try:
                kind, value = await asyncio.wait_for(queue.get(), timeout=interval_seconds)
            except asyncio.TimeoutError:
                yield heartbeat_event(run_id)
                continue
            if kind == "event":
                assert isinstance(value, AssistantEvent)
                yield value
            elif kind == "error":
                assert isinstance(value, BaseException)
                raise value
            else:
                break
    finally:
        if not producer.done():
            producer.cancel()
        try:
            await producer
        except BaseException:
            pass


def _event_content(event: AssistantEvent) -> str:
    value = event.payload.get("content")
    return value if isinstance(value, str) else ""


async def stream_prepared_run(
    session: Session,
    workspace: Workspace,
    user_id: int,
    conversation: AssistantConversation,
    run: AssistantRun,
    history: Sequence[dict[str, str]],
) -> AsyncIterator[AssistantEvent]:
    """Execute a prepared run and persist every terminal outcome."""

    controller = run_registry.register(int(run.id))
    assistant_message = session.get(AssistantMessage, run.assistant_message_id)
    if assistant_message is None:
        run_registry.unregister(int(run.id))
        raise ResourceNotFoundError("Assistant message not found")

    event_log: list[AssistantEvent] = []
    answer_parts: list[str] = []
    metadata: dict[str, Any] = {}
    usage: dict[str, Any] = {}
    last_commit = time.monotonic()
    event_count = 0
    last_cancel_poll = 0.0

    try:
        yield AssistantEvent(
            type="run_started",
            run_id=run.id,
            message_id=assistant_message.id,
            payload={"engine": conversation.mode, "status": run.status},
        )
    except BaseException:
        repository.update_run_status(run, "cancelled")
        assistant_message.status = "cancelled"
        session.commit()
        run_registry.unregister(int(run.id))
        raise

    try:
        if conversation.mode == "fast":
            engine = FastEngine()
        else:
            from app.features.assistant.engines.expert_engine import ExpertEngine

            engine = ExpertEngine(session=session, conversation=conversation, run=run)

        engine_iterator = engine.stream(
            run_id=int(run.id),
            user_id=int(user_id),
            workspace_id=int(workspace.id),
            query=str(session.get(AssistantMessage, run.user_message_id).content),
            history=history,
            controller=controller,
        )
        async for event in _engine_events_with_heartbeats(engine_iterator, int(run.id)):
            if event.run_id is None:
                event.run_id = int(run.id)
            if event.message_id is None and event.type not in {"heartbeat"}:
                event.message_id = int(assistant_message.id)
            if event.type != "heartbeat":
                event_log.append(event)

            cancel_requested = controller.cancel_event.is_set() or bool(run.cancel_requested)
            if time.monotonic() - last_cancel_poll >= 0.5:
                cancel_requested = cancel_requested or repository.is_run_cancel_requested(
                    session, int(run.id)
                )
                last_cancel_poll = time.monotonic()
            if cancel_requested:
                repository.update_run_status(run, "cancelled")
                assistant_message.status = "cancelled"
                run.pending_permission_id = None
                session.commit()
                yield AssistantEvent(
                    type="cancelled",
                    run_id=run.id,
                    message_id=assistant_message.id,
                    payload={"status": "cancelled"},
                )
                return

            if event.type == "token":
                answer_parts.append(_event_content(event))
            elif event.type == "keywords":
                metadata["keywords"] = _event_content(event)
            elif event.type == "sources":
                metadata["sources"] = event.payload.get("sources", [])
            elif event.type == "usage":
                usage.update(event.payload)
            elif event.type == "error":
                raise RuntimeError(str(event.payload.get("message") or "assistant engine error"))
            elif event.type == "file_diff":
                metadata["diff"] = event.payload.get("files", event.payload)
            elif event.type == "permission_required":
                permission_id = str(event.payload.get("permission_id") or "")
                run.pending_permission_id = permission_id or None
                run.permission_response = None
                repository.update_run_status(run, "waiting_permission")
                session.commit()
            elif event.type == "status" and run.status == "waiting_permission":
                # Do not hide the waiting state until the permission resolver
                # has handed the answer back to the engine.
                pass

            event_count += 1
            assistant_message.content = "".join(answer_parts)
            assistant_message.metadata_dict = metadata
            assistant_message.usage = usage
            if run.status == "waiting_permission" and event.type != "permission_required":
                repository.update_run_status(run, "running")
            session.flush()
            if event_count % 20 == 0 or time.monotonic() - last_commit > 1.0:
                session.commit()
                last_commit = time.monotonic()

            if event.type != "done":
                yield event

        assistant_message.content = "".join(answer_parts)
        assistant_message.status = "completed"
        assistant_message.metadata_dict = metadata
        assistant_message.usage = usage
        run.metadata_dict = repository.serialize_events_metadata(event_log)
        run.pending_permission_id = None
        repository.update_run_status(run, "completed")
        session.commit()
        yield AssistantEvent(
            type="done",
            run_id=run.id,
            message_id=assistant_message.id,
            payload={"status": "completed"},
        )
    except asyncio.CancelledError:
        repository.update_run_status(run, "cancelled")
        assistant_message.status = "cancelled"
        assistant_message.content = "".join(answer_parts)
        session.commit()
        raise
    except Exception as exc:
        assistant_message.content = "".join(answer_parts)
        assistant_message.status = "failed"
        assistant_message.metadata_dict = metadata
        assistant_message.usage = usage
        run.metadata_dict = repository.serialize_events_metadata(event_log)
        repository.update_run_status(
            run,
            "failed",
            error_code=exc.__class__.__name__,
            # Do not return provider response bodies, container paths, or
            # request details through the recovery endpoint.
            error_message="专家任务执行失败" if conversation.mode == "expert" else "快速问答执行失败",
        )
        session.commit()
        yield AssistantEvent(
            type="error",
            run_id=run.id,
            message_id=assistant_message.id,
            payload={"code": run.error_code, "message": "专家任务执行失败" if conversation.mode == "expert" else "快速问答执行失败"},
        )
    finally:
        run_registry.unregister(int(run.id))


def request_cancel(
    session: Session,
    workspace: Workspace,
    user_id: int,
    run_id: int,
) -> AssistantRun:
    run = repository.get_owned_run(session, run_id, int(workspace.id), int(user_id))
    if run is None:
        raise ResourceNotFoundError("Assistant run not found")
    if run.engine == "expert":
        assert_can_write(session, int(workspace.id), int(user_id))
    if run.status in repository.TERMINAL_RUN_STATUSES:
        return run
    run.cancel_requested = True
    session.commit()
    return run


def respond_permission(
    session: Session,
    workspace: Workspace,
    user_id: int,
    run_id: int,
    permission_id: str,
    response: str,
    remember: bool = False,
) -> AssistantRun:
    run = repository.get_owned_run(session, run_id, int(workspace.id), int(user_id))
    if run is None:
        raise ResourceNotFoundError("Assistant run not found")
    if run.engine == "expert":
        assert_can_write(session, int(workspace.id), int(user_id))
    if run.engine != "expert" or run.status != "waiting_permission":
        raise ConflictError("This run is not waiting for a permission response")
    if run.pending_permission_id != permission_id:
        raise PermissionDeniedError("Permission request does not belong to this run")
    # The provider receives only the one-shot response.  ``remember`` is
    # accepted for API compatibility but deliberately not persisted as a
    # permanent allow rule in the first version.
    del remember
    # Persist first so a different API worker can wake the engine.  The local
    # future below is only the low-latency path for same-worker requests.
    if not repository.set_permission_response(session, run_id, permission_id, response):
        raise ConflictError("Permission request has already been answered")
    run_registry.resolve_permission(run_id, permission_id, response)
    session.commit()
    return run


def handoff_to_expert(
    session: Session,
    workspace: Workspace,
    user_id: int,
    source_conversation_id: int,
    message_id: int | None = None,
) -> AssistantConversation:
    source = get_user_conversation(session, workspace, user_id, source_conversation_id)
    if source.mode != "fast":
        raise ConflictError("Only a fast conversation can be handed off")
    assert_can_write(session, int(workspace.id), int(user_id))

    messages = repository.get_messages(session, int(source.id), limit=10)
    if message_id is not None and repository.get_message_for_conversation(
        session, message_id, int(source.id)
    ) is None:
        raise ResourceNotFoundError("Handoff message not found")

    lines = [
        "以下内容来自 SKYCloud 快速问答，请在此基础上继续处理。",
        "",
    ]
    for message in messages:
        if message.role in {"user", "assistant"} and message.content.strip():
            lines.append(f"{message.role}: {message.content.strip()}")
        sources = message.metadata_dict.get("sources", [])
        if isinstance(sources, list):
            for source_item in sources[:8]:
                if isinstance(source_item, dict):
                    name = source_item.get("file_name") or source_item.get("file_id")
                    page = source_item.get("page_number")
                    if name:
                        lines.append(f"相关文件: {name}" + (f"，第 {page} 页" if page else ""))
    context = "\n".join(lines)
    target = repository.create_conversation(
        session,
        int(workspace.id),
        int(user_id),
        "expert",
        title=f"专家继续：{source.title or '快速问答'}",
        source_conversation_id=int(source.id),
    )
    repository.create_message(
        session,
        target,
        "system",
        context,
        status="completed",
        metadata={"source_conversation_id": source.id, "handoff_message_id": message_id},
    )
    session.commit()
    return target
