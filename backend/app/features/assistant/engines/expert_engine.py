"""OpenCode-backed expert execution engine."""

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator, Sequence
from typing import Any

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
    session,
    conversation: AssistantConversation,
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


class ExpertEngine:
    def __init__(self, *, session, conversation: AssistantConversation, run: AssistantRun) -> None:
        self.session = session
        self.conversation = conversation
        self.run = run

    async def _ensure_runtime(
        self,
        workspace_id: int,
        user_id: int,
    ):
        workspace = self.session.get(Workspace, workspace_id)
        if workspace is None:
            raise ServiceOperationError("工作空间不存在")
        runtime = runtime_service.get_or_create_runtime(self.session, workspace_id, user_id)
        started_now = False
        if (
            runtime.status != "running"
            or not runtime.container_id
            or not docker_service.runtime_uses_configured_image(runtime)
        ):
            yield workspace, runtime, AssistantEvent(
                "status", {"content": "正在准备专家工作区", "runtime_status": "starting"}
            )
            docker_service.start(self.session, workspace, user_id)
            started_now = True
            runtime = runtime_service.get_runtime(self.session, workspace_id, user_id)
            if runtime is None or runtime.status != "running":
                raise ServiceOperationError(runtime.error_message if runtime else "OpenCode Runtime 启动失败")
        else:
            yield workspace, runtime, AssistantEvent(
                "status", {"content": "正在连接专家工作区", "runtime_status": "running"}
            )

        # ``start`` has already injected initial config before the Runtime
        # begins listening. Existing runtimes refresh their scoped credential
        # through OpenCode's live MCP API before every expert task.
        if not started_now:
            docker_service.setup_mcp(self.session, workspace, user_id, runtime=runtime)
        yield workspace, runtime, AssistantEvent(
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
        del run_id
        if controller is None:
            raise ServiceOperationError("Expert run controller is unavailable")

        runtime_items = self._ensure_runtime(workspace_id, user_id)
        workspace = None
        runtime = None
        async for item in runtime_items:
            workspace, runtime, status_event = item
            yield status_event

        assert workspace is not None and runtime is not None
        async with _runtime_lease(int(runtime.id)):
            base_url = docker_service.get_runtime_base_url(workspace, user_id, runtime)
            password = server_password(int(runtime.id), user_id, workspace_id)
            agent = os.getenv("OPENCODE_EXPERT_AGENT", "skycloud-expert")
            session_id = self.conversation.opencode_session_id
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
                    session_id = await client.create_session(self.conversation.title)
                    self.conversation.opencode_session_id = session_id
                    self.conversation.opencode_runtime_id = runtime.id
                    self.run.opencode_session_id = session_id
                    self.session.commit()
                else:
                    self.run.opencode_session_id = session_id
                    self.conversation.opencode_runtime_id = runtime.id
                    self.session.commit()
    
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
                            _conversation_context(self.session, self.conversation, history),
                            agent,
                            no_reply=True,
                            system=EXPERT_SYSTEM_PROMPT,
                        )
                        # ``noReply`` still produces session events on some
                        # OpenCode versions. Drain that context injection before
                        # sending the user prompt so its idle event cannot end the
                        # actual task early.
                        while True:
                            try:
                                context_event = await events.__anext__()
                            except StopAsyncIteration:
                                raise OpenCodeClientError("OpenCode event stream closed during context injection")
                            context_mapped = map_opencode_event(
                                context_event, str(session_id), text_state
                            )
                            if any(item.type == "error" for item in context_mapped):
                                raise OpenCodeClientError("OpenCode context injection failed")
                            if any(item.type == "done" for item in context_mapped):
                                break
    
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
                                self.run.pending_permission_id = permission_id
                                self.run.status = "waiting_permission"
                                self.session.commit()
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
                                        if permission_future in done_tasks:
                                            response = permission_future.result()
                                            break
                                        persisted_response = repository.get_permission_response(
                                            self.session, int(self.run.id), permission_id
                                        )
                                        if persisted_response:
                                            response = persisted_response
                                            break
                                        if repository.is_run_cancel_requested(
                                            self.session, int(self.run.id)
                                        ):
                                            await client.abort_session(str(session_id))
                                            yield AssistantEvent("cancelled", {"status": "cancelled"})
                                            return
                                finally:
                                    cancel_task.cancel()
                                await client.respond_permission(
                                    str(session_id), permission_id, response, remember=False
                                )
                                self.run.pending_permission_id = None
                                self.run.permission_response = None
                                self.run.status = "running"
                                self.session.commit()
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
