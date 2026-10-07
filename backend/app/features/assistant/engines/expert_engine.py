"""OpenCode-backed expert execution engine."""

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator, Sequence
from typing import Any
from dataclasses import dataclass

from sqlalchemy.engine import Connection
from sqlalchemy.orm import sessionmaker
from starlette.concurrency import run_in_threadpool

from app.exceptions import ConflictError, ServiceOperationError
from app.features.assistant import repository
from app.features.assistant.clients.opencode_auth import server_password, server_username
from app.features.assistant.clients.opencode_client import OpenCodeClient, OpenCodeClientError
from app.features.assistant.clients.opencode_events import (
    extract_reasoning_title,
    map_opencode_event,
)
from app.features.assistant.event_protocol import AssistantEvent
from app.features.workspace import docker_service, runtime_service
from app.features.workspace.permissions import assert_can_write
import app.infra.extensions as extensions
from app.models.assistant import AssistantConversation, AssistantRun
from app.models.workspace import Workspace


EXPERT_SYSTEM_PROMPT = """你是 SKYCloud 专家助手。

你可以：
- 使用 SKYCloud MCP 搜索和读取当前工作空间文件；
- 使用 SKYCloud MCP 的 upload_file 工具直接上传文件到云盘，不依赖 curl；文本传 utf-8 内容，二进制传 Base64；
- 分析代码和文档；
- 在当前 Runtime 中执行必要命令；
- 创建分析结果和文件。

你必须：
- 只操作当前工作空间；
- 云盘写操作必须使用 SKYCloud MCP；
- 不得读取容器中的密钥、环境变量、Authorization Header 或系统敏感文件；
- 不得执行外部上传；
- 删除、覆盖、发布和高风险命令必须请求用户确认；
- 不得输出 Token、密码或内部凭证。
"""


def _permission_ui_enabled() -> bool:
    value = os.getenv("ASSISTANT_PERMISSION_UI_ENABLED")
    return value is None or value.strip().lower() not in {"0", "false", "no", "off"}


def _text_from_messages(
    messages: list[dict[str, Any]],
) -> tuple[str, str | None, dict[str, Any], str | None]:
    latest_text = ""
    provider_id: str | None = None
    usage: dict[str, Any] = {}
    title: str | None = None
    for item in messages:
        info = item.get("info") if isinstance(item, dict) else None
        if isinstance(info, dict):
            is_assistant = info.get("role") == "assistant"
            if is_assistant:
                provider_id = str(info.get("id") or provider_id or "") or provider_id
                tokens = info.get("tokens") or info.get("usage")
                if isinstance(tokens, dict):
                    usage.update(tokens)
        else:
            is_assistant = False
        if not is_assistant:
            continue
        parts = item.get("parts") if isinstance(item, dict) else None
        if not isinstance(parts, list):
            continue
        pieces: list[str] = []
        for part in parts:
            if not isinstance(part, dict):
                continue
            part_type = str(part.get("type") or "").lower()
            if part_type == "reasoning" and title is None:
                reasoning = part.get("text")
                if isinstance(reasoning, str):
                    title = extract_reasoning_title(reasoning)
            elif part_type == "text":
                value = part.get("text")
                if isinstance(value, str):
                    pieces.append(value)
        if pieces:
            latest_text = "".join(pieces)
    return latest_text, provider_id, usage, title


def _conversation_context(
    conversation: "ExpertContext",
    history: Sequence[dict[str, str]],
) -> str:
    lines = ["以下是 SKYCloud 会话上下文，仅用于恢复上下文，不要重复回答：", ""]
    for item in history[-10:]:
        role = item.get("role", "user")
        content = item.get("content", "").strip()
        if content:
            lines.append(f"{role}: {content}")
    if conversation.source_conversation_id:
        lines.append(f"\n本会话由快速模式会话 {conversation.source_conversation_id} 转交。")
    return "\n".join(lines)


_LOCAL_RUNTIME_LOCKS: dict[int, asyncio.Lock] = {}


@asynccontextmanager
async def _runtime_lease(runtime_id: int):
    """Keep one expert run active per runtime, with Redis cross-worker lock."""

    local_lock = _LOCAL_RUNTIME_LOCKS.setdefault(runtime_id, asyncio.Lock())
    if local_lock.locked():
        raise ConflictError("Expert runtime is already busy")
    await local_lock.acquire()
    redis_key = f"assistant:expert:runtime:{runtime_id}"
    lock_token = uuid.uuid4().hex
    redis_acquired = False
    try:
        try:
            result = await asyncio.to_thread(
                extensions.redis_client.set,
                redis_key,
                lock_token,
                nx=True,
                ex=int(os.getenv("ASSISTANT_RUNTIME_LOCK_TTL", "3600")),
            )
            if result is False or result is None:
                raise ConflictError("Expert runtime is already busy")
            redis_acquired = True
        except ConflictError:
            raise
        except Exception:
            # A Redis outage must not strand a local run. The local lock still
            # protects a single API worker; deployment health should alert on
            # the degraded cross-worker guarantee.
            redis_acquired = False
        yield
    finally:
        if redis_acquired:
            try:
                await asyncio.to_thread(extensions.redis_client.delete, redis_key)
            except Exception:
                pass
        local_lock.release()


@dataclass(frozen=True)
class RuntimeConnection:
    runtime_id: int
    base_url: str


@dataclass(frozen=True)
class ExpertContext:
    title: str
    opencode_session_id: str | None
    source_conversation_id: int | None


class ExpertEngine:
    def __init__(self, *, session, conversation: AssistantConversation, run: AssistantRun) -> None:
        self.conversation_id = int(conversation.id)
        self.run_id = int(run.id)
        # Runtime setup must not share the stream's mutable Session while the
        # consumer persists heartbeats/events. Create/close each Session in its
        # worker and return scalars, never commit-expired ORM instances.
        bind = session.get_bind()
        if isinstance(bind, Connection):
            bind = bind.engine
        self.runtime_session_factory = sessionmaker(bind=bind)

    def _owned_run(self, session, workspace_id: int, user_id: int):
        run = repository.get_owned_run(session, self.run_id, workspace_id, user_id)
        if run is None or run.conversation_id != self.conversation_id or run.engine != "expert":
            raise ServiceOperationError("专家任务不存在")
        assert_can_write(session, workspace_id, user_id)
        return run

    def _load_context(self, workspace_id: int, user_id: int) -> ExpertContext:
        with self.runtime_session_factory() as session:
            run = self._owned_run(session, workspace_id, user_id)
            conversation = session.get(AssistantConversation, run.conversation_id)
            return ExpertContext(conversation.title, conversation.opencode_session_id,
                                 conversation.source_conversation_id)

    def _save_provider_session(self, workspace_id: int, user_id: int, runtime_id: int, session_id: str):
        with self.runtime_session_factory() as session:
            run = self._owned_run(session, workspace_id, user_id)
            conversation = session.get(AssistantConversation, run.conversation_id)
            conversation.opencode_session_id = session_id
            conversation.opencode_runtime_id = runtime_id
            run.opencode_session_id = session_id
            session.commit()

    def _persist_permission(self, workspace_id: int, user_id: int, payload: dict):
        with self.runtime_session_factory() as session:
            self._owned_run(session, workspace_id, user_id)
            run = repository.lock_run(session, self.run_id)
            if run.status in repository.TERMINAL_RUN_STATUSES or run.cancel_requested:
                raise ConflictError("专家任务已取消或结束")
            repository.set_pending_permission(run, payload)
            session.commit()

    def _permission_state(self, workspace_id: int, user_id: int, permission_id: str):
        with self.runtime_session_factory() as session:
            run = self._owned_run(session, workspace_id, user_id)
            cancelled = run.cancel_requested or run.status in repository.TERMINAL_RUN_STATUSES
            response = run.permission_response if run.pending_permission_id == permission_id else None
            return response, bool(cancelled)

    def _finish_permission(self, workspace_id: int, user_id: int, permission_id: str) -> bool:
        with self.runtime_session_factory() as session:
            self._owned_run(session, workspace_id, user_id)
            run = repository.lock_run(session, self.run_id)
            if (run.status in repository.TERMINAL_RUN_STATUSES or run.cancel_requested
                    or run.pending_permission_id != permission_id):
                return False
            repository.clear_pending_permission(run)
            run.status = "running"
            session.commit()
            return True

    def _runtime_needs_start(self, workspace_id: int, user_id: int) -> bool:
        with self.runtime_session_factory() as session:
            workspace = session.get(Workspace, workspace_id)
            if workspace is None:
                raise ServiceOperationError("工作空间不存在")
            assert_can_write(session, workspace_id, user_id)
            runtime = runtime_service.get_or_create_runtime(session, workspace_id, user_id)
            needs_start = (
                runtime.status != "running"
                or not runtime.container_id
                or not docker_service.runtime_uses_configured_image(runtime)
            )
            session.commit()
            return needs_start

    def _prepare_runtime(self, workspace_id: int, user_id: int, needs_start: bool) -> RuntimeConnection:
        with self.runtime_session_factory() as session:
            workspace = session.get(Workspace, workspace_id)
            if workspace is None:
                raise ServiceOperationError("工作空间不存在")
            assert_can_write(session, workspace_id, user_id)
            if needs_start:
                docker_service.start(session, workspace, user_id)
            runtime = runtime_service.get_runtime(session, workspace_id, user_id)
            if runtime is None or runtime.status != "running":
                raise ServiceOperationError(runtime.error_message if runtime else "OpenCode Runtime 启动失败")
            # start already injects initial config. Existing runtimes refresh
            # their scoped credentials through the live MCP API per task.
            if not needs_start:
                docker_service.setup_mcp(session, workspace, user_id, runtime=runtime)
            return RuntimeConnection(
                runtime_id=int(runtime.id),
                base_url=docker_service.get_runtime_base_url(workspace, user_id, runtime),
            )

    async def _ensure_runtime(self, workspace_id: int, user_id: int):
        needs_start = await run_in_threadpool(self._runtime_needs_start, workspace_id, user_id)
        yield None, AssistantEvent(
            "status", {
                "content": "正在准备专家工作区" if needs_start else "正在连接专家工作区",
                "runtime_status": "starting" if needs_start else "running",
            }
        )
        connection = await run_in_threadpool(self._prepare_runtime, workspace_id, user_id, needs_start)
        yield connection, AssistantEvent(
            "status", {"content": "正在检查 OpenCode 服务", "runtime_status": "running"}
        )

    async def stream(
        self,
        *,
        run_id: int,
        user_id: int,
        workspace_id: int,
        query: str,
        history: Sequence[dict[str, str]],
        controller: Any = None,
    ) -> AsyncIterator[AssistantEvent]:
        if controller is None:
            raise ServiceOperationError("Expert run controller is unavailable")

        connection = None
        async for prepared_connection, status_event in self._ensure_runtime(workspace_id, user_id):
            if prepared_connection is not None:
                connection = prepared_connection
            yield status_event

        assert connection is not None
        async with _runtime_lease(connection.runtime_id):
            base_url = connection.base_url
            password = server_password(connection.runtime_id, user_id, workspace_id)
            agent = os.getenv("OPENCODE_EXPERT_AGENT", "skycloud-expert")
            context = await run_in_threadpool(self._load_context, workspace_id, user_id)
            session_id = context.opencode_session_id
            emitted_text = ""
            emitted_title: str | None = None
            text_state: dict[str, str] = {"__user_query": query}
            done_seen = False

            async with OpenCodeClient(
                base_url,
                server_username(),
                password,
                timeout=float(os.getenv("OPENCODE_HTTP_TIMEOUT", "30")),
            ) as client:
                health = await client.health()
                if health and health.get("healthy") is False:
                    raise ServiceOperationError("OpenCode 服务未就绪")
                mcp_status = await client.get_mcp_status()
                skycloud_status = mcp_status.get("SKYCLOUD") if isinstance(mcp_status, dict) else None
                if isinstance(skycloud_status, dict) and str(skycloud_status.get("status", "")).lower() in {
                    "error",
                    "disabled",
                }:
                    raise ServiceOperationError("SKYCloud MCP 未就绪")
                yield AssistantEvent("status", {"content": "专家服务已就绪", "runtime_status": "running"})
    
                session_missing = False
                if session_id:
                    session_missing = await client.get_session(session_id) is None
                created_session = not session_id or session_missing
                if created_session:
                    session_id = await client.create_session(context.title)
                await run_in_threadpool(
                    self._save_provider_session, workspace_id, user_id, connection.runtime_id, str(session_id)
                )

                controller.set_abort_callback(lambda: client.abort_session(str(session_id)))
    
                events = client.stream_events().__aiter__()
                try:
                    # Establish the event stream before prompting so the first
                    # tool/status event cannot be lost.
                    try:
                        first_event = await events.__anext__()
                        for mapped in map_opencode_event(first_event, str(session_id), text_state):
                            if mapped.type != "status" or mapped.payload.get("content") != "专家服务已连接":
                                yield mapped
                    except StopAsyncIteration:
                        raise OpenCodeClientError("OpenCode event stream closed before connection")
    
                    if created_session and history:
                        await client.prompt_async(
                            str(session_id),
                            _conversation_context(context, history),
                            agent,
                            no_reply=True,
                            system=EXPERT_SYSTEM_PROMPT,
                        )
                    await client.prompt_async(
                        str(session_id),
                        query,
                        agent,
                        no_reply=False,
                        system=EXPERT_SYSTEM_PROMPT,
                    )
    
                    async for provider_event in events:
                        mapped_events = map_opencode_event(
                            provider_event, str(session_id), text_state
                        )
                        for mapped in mapped_events:
                            if mapped.type == "token":
                                text = mapped.payload.get("content")
                                if isinstance(text, str):
                                    emitted_text += text
                            elif mapped.type == "permission_required":
                                permission_id = str(mapped.payload.get("permission_id") or "")
                                if not _permission_ui_enabled():
                                    await client.respond_permission(
                                        str(session_id), permission_id, "reject", remember=False
                                    )
                                    yield AssistantEvent(
                                        "status",
                                        {"content": "权限确认功能未开启，已拒绝高风险操作"},
                                    )
                                    continue
                                permission_future = controller.wait_for_permission(permission_id)
                                await run_in_threadpool(
                                    self._persist_permission, workspace_id, user_id, mapped.payload
                                )
                                yield mapped
                                cancel_task = asyncio.create_task(controller.cancel_event.wait())
                                try:
                                    while True:
                                        done_tasks, _ = await asyncio.wait(
                                            {permission_future, cancel_task},
                                            timeout=0.5,
                                            return_when=asyncio.FIRST_COMPLETED,
                                        )
                                        if cancel_task in done_tasks:
                                            yield AssistantEvent("cancelled", {"status": "cancelled"})
                                            return
                                        persisted_response, cancelled = await run_in_threadpool(
                                            self._permission_state, workspace_id, user_id, permission_id
                                        )
                                        if cancelled:
                                            await client.abort_session(str(session_id))
                                            yield AssistantEvent("cancelled", {"status": "cancelled"})
                                            return
                                        if persisted_response:
                                            response = persisted_response
                                            break
                                        if permission_future in done_tasks:
                                            raise ConflictError("权限回复未持久化或已失效")
                                except BaseException:
                                    # A revoked role, DB failure or disconnected
                                    # waiter must not leave a live provider task.
                                    try:
                                        await asyncio.wait_for(client.abort_session(str(session_id)), timeout=5)
                                    except (Exception, asyncio.CancelledError):
                                        pass
                                    raise
                                finally:
                                    cancel_task.cancel()
                                    await asyncio.gather(cancel_task, return_exceptions=True)
                                await client.respond_permission(
                                    str(session_id), permission_id, response, remember=False
                                )
                                if not await run_in_threadpool(
                                    self._finish_permission, workspace_id, user_id, permission_id
                                ):
                                    await client.abort_session(str(session_id))
                                    yield AssistantEvent("cancelled", {"status": "cancelled"})
                                    return
                                yield AssistantEvent("status", {"content": "权限已响应，继续执行"})
                                continue
                            elif mapped.type == "done":
                                done_seen = True
                            yield mapped
                        if done_seen:
                            break
                except OpenCodeClientError:
                    # If the event connection dropped after the provider accepted
                    # the prompt, rebuild the answer from the durable message API.
                    messages = await client.get_messages(str(session_id))
                    final_text, provider_id, usage, title = _text_from_messages(messages)
                    if final_text and not emitted_text:
                        yield AssistantEvent("token", {"content": final_text})
                        emitted_text = final_text
                    if title and not emitted_title:
                        yield AssistantEvent("title", {"content": title})
                        emitted_title = title
                    if provider_id or usage:
                        yield AssistantEvent(
                            "usage",
                            {"provider_message_id": provider_id, **usage},
                        )
                    if not final_text:
                        raise
    
                messages = await client.get_messages(str(session_id))
                final_text, provider_id, usage, title = _text_from_messages(messages)
                if title and not emitted_title:
                    yield AssistantEvent("title", {"content": title})
                    emitted_title = title
                if final_text:
                    if final_text.startswith(emitted_text):
                        suffix = final_text[len(emitted_text):]
                    elif not emitted_text:
                        suffix = final_text
                    else:
                        suffix = ""
                    if suffix:
                        yield AssistantEvent("token", {"content": suffix})
                if provider_id or usage:
                    yield AssistantEvent("usage", {"provider_message_id": provider_id, **usage})
    
                diffs = await client.get_diff(str(session_id))
                if diffs:
                    yield AssistantEvent("file_diff", {"files": diffs})
                if controller.cancel_event.is_set():
                    yield AssistantEvent("cancelled", {"status": "cancelled"})
                    return
                yield AssistantEvent("done", {"status": "completed"})
