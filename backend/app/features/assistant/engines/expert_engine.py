"""OpenCode-backed expert execution engine."""

import asyncio
import os
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from contextlib import aclosing, suppress
from dataclasses import dataclass
from typing import NoReturn

from runtime_common.json_types import JsonObject, JsonValue
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session, sessionmaker

from app.exceptions import ConflictError, ServiceOperationError
from app.features.assistant import repository
from app.features.assistant.clients.opencode_auth import (
    server_password,
    server_username,
)
from app.features.assistant.clients.opencode_client import (
    OpenCodeActionError,
    OpenCodeClient,
    OpenCodeClientError,
)
from app.features.assistant.clients.opencode_events import (
    extract_reasoning_title,
    map_opencode_event,
)
from app.features.assistant.engines.base import EngineRequest
from app.features.assistant.engines.runtime_lease import (
    runtime_lease as _runtime_lease,
)
from app.features.assistant.event_protocol import AssistantEvent
from app.features.assistant.run_control import RunController
from app.features.workspace import docker_service, runtime_service
from app.features.workspace.permissions import assert_can_write
from app.infra.owned_work import run_owned
from app.models.assistant import (
    AssistantConversation,
    AssistantPermission,
    AssistantRun,
)
from app.models.workspace import Workspace

EXPERT_SYSTEM_PROMPT = (
    "你是 SKYCloud 专家助手。\n"
    "\n"
    "你可以:\n"
    "- 使用 SKYCloud MCP 搜索和读取当前工作空间文件;\n"
    "- 使用 SKYCloud MCP 的 upload_file 工具直接上传文件到云盘,不依赖 curl;"
    "文本传 utf-8 内容,二进制传 Base64;\n"
    "- 分析代码和文档;\n"
    "- 在当前 Runtime 中执行必要命令;\n"
    "- 创建分析结果和文件。\n"
    "\n"
    "你必须:\n"
    "- 只操作当前工作空间;\n"
    "- 云盘写操作必须使用 SKYCloud MCP;\n"
    "- 不得读取容器中的密钥、环境变量、Authorization Header 或系统敏感文件;\n"
    "- 不得执行外部上传;\n"
    "- 删除、覆盖、发布和高风险命令必须请求用户确认;\n"
    "- 不得输出 Token、密码或内部凭证。\n"
)


def _permission_ui_enabled() -> bool:
    value = os.getenv("ASSISTANT_PERMISSION_UI_ENABLED")
    return value is None or value.strip().lower() not in {"0", "false", "no", "off"}


def _mcp_unavailable(status: JsonObject) -> bool:
    """Return whether the SKYCloud MCP server is unusable for this run."""
    skycloud = status.get("SKYCLOUD")
    if not isinstance(skycloud, dict):
        return False
    state = str(skycloud.get("status", "")).lower()
    return state in {"error", "disabled"}


def _merge_usage(info: JsonObject, usage: JsonObject) -> str | None:
    """Merge one assistant message's token usage and return its provider id."""
    provider_id = info.get("id")
    tokens = info.get("tokens") or info.get("usage")
    if isinstance(tokens, dict):
        usage.update(tokens)
    if not provider_id:
        return None
    return str(provider_id)


def _message_text(parts: Sequence[JsonValue]) -> tuple[str, str | None]:
    """Return the joined text and first reasoning title of one message."""
    pieces: list[str] = []
    title: str | None = None
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
    return "".join(pieces), title


def _text_from_messages(
    messages: Sequence[JsonObject],
) -> tuple[str, str | None, JsonObject, str | None]:
    """Return the newest assistant text, provider id, usage, and title."""
    latest_text = ""
    provider_id: str | None = None
    usage: JsonObject = {}
    title: str | None = None
    for item in messages:
        info = item.get("info")
        if not isinstance(info, dict) or info.get("role") != "assistant":
            continue
        provider_id = _merge_usage(info, usage) or provider_id
        parts = item.get("parts")
        if not isinstance(parts, list):
            continue
        text, message_title = _message_text(parts)
        if title is None:
            title = message_title
        if text:
            latest_text = text
    return latest_text, provider_id, usage, title


def _is_query_part(part: JsonValue, query: str) -> bool:
    """Return whether a provider message part repeats the submitted query."""
    if not isinstance(part, dict):
        return False
    return part.get("type") == "text" and part.get("text") == query


def _query_parent_ids(
    messages: Sequence[JsonObject],
    previous_ids: set[str],
    query: str,
) -> set[str]:
    """Return user message ids that carry this newly submitted query."""
    parents: set[str] = set()
    for item in messages:
        info = item.get("info")
        if not isinstance(info, dict) or info.get("role") != "user":
            continue
        message_id = info.get("id")
        parts = item.get("parts")
        if not isinstance(message_id, str) or not isinstance(parts, list):
            continue
        if message_id in previous_ids:
            continue
        if any(_is_query_part(part, query) for part in parts):
            parents.add(message_id)
    return parents


def _completed_reply(
    messages: Sequence[JsonObject],
    previous_ids: set[str],
    parents: set[str],
) -> tuple[str, str | None, JsonObject, str | None]:
    """Return the completed assistant message belonging to this query."""
    completed: list[JsonObject] = []
    for item in messages:
        info = item.get("info")
        if not isinstance(info, dict):
            continue
        message_id = info.get("id")
        if info.get("role") != "assistant":
            continue
        if not isinstance(message_id, str) or not message_id:
            continue
        if message_id in previous_ids:
            continue
        timing = info.get("time")
        finish = info.get("finish")
        if (
            info.get("parentID") in parents
            and not info.get("error")
            and isinstance(timing, dict)
            and timing.get("completed")
            and finish not in {None, "tool-calls", "unknown"}
        ):
            completed.append(item)
    return _text_from_messages(completed)


def _current_reply(
    messages: Sequence[JsonObject],
    previous_ids: set[str],
    query: str,
) -> tuple[str, str | None, JsonObject, str | None]:
    """Recover only a completed answer parented to this newly submitted query."""
    parents = _query_parent_ids(messages, previous_ids, query)
    return _completed_reply(messages, previous_ids, parents)


def _previous_message_ids(messages: Sequence[JsonObject]) -> set[str]:
    """Return the durable provider message ids recorded before the prompt."""
    previous_ids: set[str] = set()
    for message in messages:
        info = message.get("info")
        message_id = info.get("id") if isinstance(info, dict) else None
        if not isinstance(message_id, str):
            invalid = "OpenCode returned invalid prior message identity"
            raise OpenCodeClientError(invalid)
        previous_ids.add(message_id)
    return previous_ids


def _answer_suffix(final_text: str, emitted_text: str) -> str:
    """Return the part of the durable answer that was not streamed yet."""
    if final_text.startswith(emitted_text):
        return final_text[len(emitted_text):]
    if not emitted_text:
        return final_text
    return ""


def _raise_permission_not_persisted() -> NoReturn:
    """Fail when a resolved permission was never durably recorded."""
    message = "权限回复未持久化或已失效"
    raise ConflictError(message)


async def _abort_quietly(client: OpenCodeClient, session_id: str) -> None:
    """Abort one provider session without masking the primary failure."""
    with suppress(Exception, asyncio.CancelledError):
        await asyncio.wait_for(client.abort_session(session_id), timeout=5)


@dataclass(frozen=True)
class RuntimeConnection:
    """Resolved OpenCode runtime endpoint for one expert run."""

    runtime_id: int
    base_url: str
    container_id: str


@dataclass(frozen=True)
class ExpertContext:
    """Durable conversation state reused by an expert provider session."""

    title: str | None
    opencode_session_id: str | None
    source_conversation_id: int | None


def _conversation_context(
    conversation: ExpertContext,
    history: Sequence[dict[str, str]],
) -> str:
    lines = ["以下是 SKYCloud 会话上下文,仅用于恢复上下文,不要重复回答:", ""]
    for item in history[-10:]:
        role = item.get("role", "user")
        content = item.get("content", "").strip()
        if content:
            lines.append(f"{role}: {content}")
    if conversation.source_conversation_id:
        source = conversation.source_conversation_id
        lines.append(f"\n本会话由快速模式会话 {source} 转交。")
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class RunInputs:
    """Identifiers and prompt inputs for one expert run."""

    workspace_id: int
    user_id: int
    query: str
    history: Sequence[dict[str, str]]


@dataclass(frozen=True, slots=True)
class PermissionDispatch:
    """Identity of one permission answer that a worker may dispatch."""

    provider_session_id: str
    permission_id: str
    response: str

@dataclass(frozen=True, slots=True)
class PermissionResponse:
    """A permission response that is ready or awaiting durable confirmation."""

    response: str | None
    auto_rejected: bool = False


@dataclass(frozen=True, slots=True)
class PermissionWait:
    """Provider event and future for one UI-mediated permission decision."""

    event: AssistantEvent
    future: asyncio.Future[str]

@dataclass(frozen=True, slots=True)
class ProviderSession:
    """Provider session selected for the current exchange."""

    session_id: str
    created: bool


@dataclass(frozen=True, slots=True)
class ReplyRecovery:
    """Durable answer recovered after the provider event stream dropped."""

    text: str
    provider_id: str | None
    usage: JsonObject
    title: str | None


@dataclass(slots=True)
class StreamProgress:
    """Mutable progress of one streamed provider exchange."""

    emitted_text: str = ""
    emitted_title: str | None = None
    previous_ids: set[str] | None = None
    done_seen: bool = False
    permission_rejected: bool = False
    cancelled: bool = False


@dataclass(frozen=True, slots=True)
class Exchange:
    """Live provider connection plus the immutable inputs of one exchange."""

    inputs: RunInputs
    runtime: RuntimeConnection
    controller: RunController
    client: OpenCodeClient
    provider: ProviderSession
    context: ExpertContext
    agent: str
    events: AsyncIterator[JsonObject]
    text_state: dict[str, str]


class ExpertEngine:
    """OpenCode-backed engine for one expert assistant run."""

    def __init__(
        self,
        *,
        session: Session,
        conversation: AssistantConversation,
        run: AssistantRun,
    ) -> None:
        """Bind the engine to one owned run and its durable conversation."""
        self.conversation_id = int(conversation.id)
        self.run_id = int(run.id)
        # Runtime setup must not share the stream's mutable Session while the
        # consumer persists heartbeats/events. Create/close each Session in its
        # worker and return scalars, never commit-expired ORM instances.
        bind = session.get_bind()
        if isinstance(bind, Connection):
            bind = bind.engine
        self.runtime_session_factory: sessionmaker[Session] = sessionmaker(
            bind=bind,
        )

    def _owned_run(
        self,
        session: Session,
        workspace_id: int,
        user_id: int,
    ) -> AssistantRun:
        """Return the run when the caller still owns this expert run."""
        run = repository.get_owned_run(
            session, self.run_id, workspace_id, user_id,
        )
        if (
            run is None
            or run.conversation_id != self.conversation_id
            or run.engine != "expert"
        ):
            message = "专家任务不存在"
            raise ServiceOperationError(message)
        assert_can_write(session, workspace_id, user_id)
        return run

    def _active_owned_run(
        self,
        session: Session,
        workspace_id: int,
        user_id: int,
    ) -> AssistantRun:
        """Return the owned run only while it is still active."""
        self._owned_run(session, workspace_id, user_id)
        run = repository.lock_run(session, self.run_id)
        if (
            run is None
            or run.status in repository.TERMINAL_RUN_STATUSES
            or run.cancel_requested
        ):
            message = "专家任务已取消或结束"
            raise ConflictError(message)
        return run

    def _load_context(
        self,
        workspace_id: int,
        user_id: int,
    ) -> ExpertContext:
        """Return the durable conversation context for this run."""
        with self.runtime_session_factory() as session:
            run = self._active_owned_run(session, workspace_id, user_id)
            conversation = session.get(
                AssistantConversation, run.conversation_id,
            )
            if conversation is None:
                message = "专家会话不存在"
                raise ServiceOperationError(message)
            return ExpertContext(
                conversation.title,
                conversation.opencode_session_id,
                conversation.source_conversation_id,
            )

    def _save_provider_session(
        self,
        workspace_id: int,
        user_id: int,
        runtime_id: int,
        session_id: str,
    ) -> None:
        """Persist the provider session identity for this run."""
        with self.runtime_session_factory() as session:
            run = self._active_owned_run(session, workspace_id, user_id)
            conversation = session.get(
                AssistantConversation, run.conversation_id,
            )
            if conversation is None:
                message = "专家会话不存在"
                raise ServiceOperationError(message)
            conversation.opencode_session_id = session_id
            conversation.opencode_runtime_id = runtime_id
            run.opencode_session_id = session_id
            session.commit()

    def _save_runtime_lease(
        self,
        workspace_id: int,
        user_id: int,
        token: str,
        runtime_id: int,
        container_id: str,
    ) -> None:
        """Record the lease token before Redis grants runtime ownership."""
        with self.runtime_session_factory() as session:
            run = self._active_owned_run(session, workspace_id, user_id)
            run.runtime_lease_token = token
            run.runtime_lease_runtime_id = runtime_id
            run.runtime_lease_container_id = container_id
            session.commit()

    def _persist_permission(
        self,
        workspace_id: int,
        user_id: int,
        payload: JsonObject,
    ) -> AssistantPermission:
        """Register one durable permission request for this run."""
        with self.runtime_session_factory() as session:
            run = self._owned_run(session, workspace_id, user_id)
            permission = repository.register_permission(session, run, payload)
            session.commit()
            session.refresh(permission)
            return permission

    def _permission_state(
        self,
        workspace_id: int,
        user_id: int,
        permission_id: str,
    ) -> tuple[str | None, bool]:
        """Return the durable response and cancellation state of the run."""
        with self.runtime_session_factory() as session:
            run = self._owned_run(session, workspace_id, user_id)
            permission = repository.permission_for_run(
                session,
                self.run_id,
                permission_id,
            )
            cancelled = (
                run.cancel_requested
                or run.status in repository.TERMINAL_RUN_STATUSES
            )
            response = (
                permission.response
                if permission is not None and permission.state == "answered"
                else None
            )
            return response, bool(cancelled)

    def _reject_pending_permission(
        self,
        workspace_id: int,
        user_id: int,
        permission_id: str,
    ) -> str | None:
        """Persist the UI-disabled rejection before provider dispatch."""
        with self.runtime_session_factory() as session:
            run = self._owned_run(session, workspace_id, user_id)
            permission = repository.permission_for_run(
                session,
                self.run_id,
                permission_id,
            )
            if permission is None:
                message = "Permission request is no longer current"
                raise ConflictError(message)
            if permission.state == "answered":
                return permission.response
            if permission.state != "pending":
                return None
            if not repository.answer_permission(
                session,
                run,
                permission_id,
                "reject",
            ):
                message = "Permission request is no longer pending"
                raise ConflictError(message)
            session.commit()
            return "reject"

    def _dispatch_permission(
        self,
        workspace_id: int,
        user_id: int,
        runtime: RuntimeConnection,
        dispatch: PermissionDispatch,
    ) -> None:
        """Dispatch a permission response after locking its SQL identity."""
        # SQL and this HTTP client both belong to this worker thread. Parent
        # locks serialize grant dispatch against demotion/delete/replacement.
        with self.runtime_session_factory() as session:
            if session.get_bind().dialect.name == "postgresql":
                session.execute(text("SET LOCAL lock_timeout = '5s'"))
            run = repository.lock_permission_dispatch(
                session,
                repository.PermissionDispatchParams(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    conversation_id=self.conversation_id,
                    run_id=self.run_id,
                    runtime_id=runtime.runtime_id,
                    container_id=runtime.container_id,
                    provider_session_id=dispatch.provider_session_id,
                    permission_id=dispatch.permission_id,
                    response=dispatch.response,
                ),
            )
            permission = repository.permission_for_run(
                session,
                self.run_id,
                dispatch.permission_id,
            )
            if permission is None:
                message = "Permission response is no longer current"
                raise ConflictError(message)
            password = server_password(
                runtime.runtime_id,
                user_id,
                workspace_id,
            )

            async def respond() -> None:
                async with OpenCodeClient(
                    runtime.base_url,
                    server_username(),
                    password,
                    timeout=5,
                ) as client:
                    try:
                        await asyncio.wait_for(
                            client.respond_permission(
                                dispatch.provider_session_id,
                                dispatch.permission_id,
                                dispatch.response,
                                remember=False,
                            ),
                            5,
                        )
                    except TimeoutError:
                        message = "OpenCode permission dispatch timed out"
                        raise OpenCodeActionError(message) from None

            asyncio.run(respond())
            repository.mark_permission_dispatched(session, run, permission)
            session.commit()

    def _runtime_needs_start(self, workspace_id: int, user_id: int) -> bool:
        """Return whether the runtime must be started for this run."""
        with self.runtime_session_factory() as session:
            workspace = session.get(Workspace, workspace_id)
            if workspace is None:
                message = "工作空间不存在"
                raise ServiceOperationError(message)
            assert_can_write(session, workspace_id, user_id)
            runtime = runtime_service.get_or_create_runtime(
                session, workspace_id, user_id,
            )
            needs_start = (
                runtime.status != "running"
                or not runtime.container_id
                or not docker_service.runtime_uses_configured_image(runtime)
            )
            session.commit()
            return needs_start

    def _prepare_runtime(
        self,
        workspace_id: int,
        user_id: int,
        *,
        needs_start: bool,
    ) -> RuntimeConnection:
        """Start or reuse the runtime and return its relay endpoint."""
        with self.runtime_session_factory() as session:
            workspace = session.get(Workspace, workspace_id)
            if workspace is None:
                message = "工作空间不存在"
                raise ServiceOperationError(message)
            assert_can_write(session, workspace_id, user_id)
            if needs_start:
                docker_service.start(session, workspace, user_id)
            runtime = runtime_service.get_runtime(
                session, workspace_id, user_id,
            )
            if runtime is None or runtime.status != "running":
                failed = (
                    "OpenCode Runtime 启动失败"
                    if runtime is None
                    else runtime.error_message
                )
                raise ServiceOperationError(failed)
            # start already injects initial config. Existing runtimes refresh
            # their scoped credentials through the live MCP API per task.
            if not needs_start:
                docker_service.setup_mcp(
                    session, workspace, user_id, runtime=runtime,
                )
            return RuntimeConnection(
                runtime_id=int(runtime.id),
                container_id=str(runtime.container_id),
                base_url=docker_service.get_runtime_base_url(
                    workspace, user_id, runtime,
                ),
            )

    async def _ensure_runtime(
        self,
        workspace_id: int,
        user_id: int,
    ) -> AsyncGenerator[tuple[RuntimeConnection | None, AssistantEvent], None]:
        """Yield the runtime status events that lead to a live connection."""
        needs_start = await run_owned(
            self._runtime_needs_start, workspace_id, user_id,
        )
        content = (
            "正在准备专家工作区" if needs_start else "正在连接专家工作区"
        )
        yield None, AssistantEvent(
            "status",
            {
                "content": content,
                "runtime_status": "starting" if needs_start else "running",
            },
        )
        connection = await run_owned(
            self._prepare_runtime,
            workspace_id,
            user_id,
            needs_start=needs_start,
        )
        yield connection, AssistantEvent(
            "status",
            {"content": "正在检查 OpenCode 服务", "runtime_status": "running"},
        )

    async def stream(
        self,
        request: EngineRequest,
    ) -> AsyncGenerator[AssistantEvent, None]:
        """Stream one expert run through its leased OpenCode runtime."""
        controller = request.controller
        if controller is None:
            message = "Expert run controller is unavailable"
            raise ServiceOperationError(message)
        inputs = RunInputs(
            workspace_id=request.workspace_id,
            user_id=request.user_id,
            query=request.query,
            history=request.history,
        )
        prepared: RuntimeConnection | None = None
        async for connection, status_event in self._ensure_runtime(
            inputs.workspace_id,
            inputs.user_id,
        ):
            if connection is not None:
                prepared = connection
            yield status_event
        runtime = prepared
        if runtime is None:
            message = "Expert runtime preparation did not return a connection"
            raise AssertionError(message)

        async def prepare_lease(token: str) -> None:
            await run_owned(
                self._save_runtime_lease,
                inputs.workspace_id,
                inputs.user_id,
                token,
                runtime.runtime_id,
                runtime.container_id,
            )

        async with _runtime_lease(
            runtime.runtime_id,
            on_lost=controller.request_cancel,
            before_acquire=prepare_lease,
        ):
            events = self._stream_provider(inputs, runtime, controller)
            async with aclosing(events):
                async for event in events:
                    yield event

    async def _require_provider_ready(self, client: OpenCodeClient) -> None:
        """Fail before prompting when the runtime or MCP bridge is unready."""
        health = await client.health()
        if health.get("healthy") is False:
            message = "OpenCode 服务未就绪"
            raise ServiceOperationError(message)
        status = await client.get_mcp_status()
        if _mcp_unavailable(status):
            message = "SKYCloud MCP 未就绪"
            raise ServiceOperationError(message)

    async def _open_provider_session(
        self,
        client: OpenCodeClient,
        context: ExpertContext,
        runtime: RuntimeConnection,
        inputs: RunInputs,
    ) -> ProviderSession:
        """Resume the durable provider session or create a fresh one."""
        session_id = context.opencode_session_id
        missing = False
        if session_id:
            missing = await client.get_session(session_id) is None
        created = not session_id or missing
        if created:
            session_id = await client.create_session(context.title)
        await run_owned(
            self._save_provider_session,
            inputs.workspace_id,
            inputs.user_id,
            runtime.runtime_id,
            str(session_id),
        )
        return ProviderSession(session_id=str(session_id), created=created)

    async def _stream_provider(
        self,
        inputs: RunInputs,
        runtime: RuntimeConnection,
        controller: RunController,
    ) -> AsyncGenerator[AssistantEvent, None]:
        """Run the provider conversation for one leased expert runtime."""
        password = server_password(
            runtime.runtime_id, inputs.user_id, inputs.workspace_id,
        )
        agent = os.getenv("OPENCODE_EXPERT_AGENT", "skycloud-expert")
        context = await run_owned(
            self._load_context, inputs.workspace_id, inputs.user_id,
        )
        timeout = float(os.getenv("OPENCODE_HTTP_TIMEOUT", "30"))
        async with OpenCodeClient(
            runtime.base_url,
            server_username(),
            password,
            timeout=timeout,
        ) as client:
            await self._require_provider_ready(client)
            yield AssistantEvent(
                "status",
                {"content": "专家服务已就绪", "runtime_status": "running"},
            )
            provider = await self._open_provider_session(
                client, context, runtime, inputs,
            )

            async def abort_provider_session() -> bool:
                # The event stream may finish/close its client while the
                # remote abort is in flight. Give this bounded control RPC
                # its own transport lifetime.
                async with OpenCodeClient(
                    runtime.base_url,
                    server_username(),
                    password,
                    timeout=5,
                ) as abort_client:
                    return await abort_client.abort_session(provider.session_id)

            controller.set_abort_callback(abort_provider_session)
            exchange = Exchange(
                inputs=inputs,
                runtime=runtime,
                controller=controller,
                client=client,
                provider=provider,
                context=context,
                agent=agent,
                events=client.stream_events(),
                text_state={"__user_query": inputs.query},
            )
            progress = StreamProgress()
            events = self._exchange_events(exchange, progress)
            async with aclosing(events):
                async for event in events:
                    yield event

    async def _exchange_events(
        self,
        exchange: Exchange,
        progress: StreamProgress,
    ) -> AsyncGenerator[AssistantEvent, None]:
        """Relay provider events, then emit the durable final answer."""
        try:
            async with aclosing(self._open_exchange(exchange, progress)) as events:
                async for event in events:
                    yield event
            async with aclosing(self._relay_events(exchange, progress)) as events:
                async for event in events:
                    yield event
            if progress.cancelled:
                return
        except OpenCodeActionError:
            # An unacknowledged permission must not clear SQL state or
            # recover an older answer as if this control action succeeded.
            await _abort_quietly(exchange.client, exchange.provider.session_id)
            raise
        except OpenCodeClientError:
            # If the event connection dropped after the provider accepted
            # the prompt, rebuild the answer from the durable message API.
            if progress.previous_ids is None:
                raise
            recovery = await self._recover_reply(exchange, progress)
            async with aclosing(_recovery_events(recovery, progress)) as events:
                async for event in events:
                    yield event
            if not recovery.text:
                raise
        events = self._finalize_exchange(exchange, progress)
        async with aclosing(events):
            async for event in events:
                yield event

    async def _open_exchange(
        self,
        exchange: Exchange,
        progress: StreamProgress,
    ) -> AsyncGenerator[AssistantEvent, None]:
        """Open the event stream, then submit context and the new query."""
        try:
            first_event = await exchange.events.__anext__()
        except StopAsyncIteration:
            message = "OpenCode event stream closed before connection"
            raise OpenCodeClientError(message) from None
        mapped_first = map_opencode_event(
            first_event,
            exchange.provider.session_id,
            exchange.text_state,
        )
        for mapped in mapped_first:
            connected = (
                mapped.type == "status"
                and mapped.payload.get("content") == "专家服务已连接"
            )
            if not connected:
                yield mapped
        history = exchange.inputs.history
        if exchange.provider.created and history:
            await exchange.client.prompt_async(
                exchange.provider.session_id,
                _conversation_context(exchange.context, history),
                exchange.agent,
                no_reply=True,
                system=EXPERT_SYSTEM_PROMPT,
            )
        prior_messages = await exchange.client.get_messages(
            exchange.provider.session_id,
        )
        progress.previous_ids = _previous_message_ids(prior_messages)
        await exchange.client.prompt_async(
            exchange.provider.session_id,
            exchange.inputs.query,
            exchange.agent,
            no_reply=False,
            system=EXPERT_SYSTEM_PROMPT,
        )

    async def _relay_events(
        self,
        exchange: Exchange,
        progress: StreamProgress,
    ) -> AsyncGenerator[AssistantEvent, None]:
        """Map provider events until the provider reports this turn done."""
        async for provider_event in exchange.events:
            mapped_events = map_opencode_event(
                provider_event,
                exchange.provider.session_id,
                exchange.text_state,
            )
            for mapped in mapped_events:
                if mapped.type == "token":
                    text = mapped.payload.get("content")
                    if isinstance(text, str):
                        progress.emitted_text += text
                elif mapped.type == "permission_required":
                    events = self._resolve_permission(exchange, progress, mapped)
                    async with aclosing(events):
                        async for event in events:
                            yield event
                    if progress.cancelled:
                        return
                    continue
                elif mapped.type == "done":
                    progress.done_seen = True
                yield mapped
            if progress.done_seen:
                break

    async def _wait_for_permission_response(
        self,
        exchange: Exchange,
        progress: StreamProgress,
        permission_id: str,
        permission_future: asyncio.Future[str],
    ) -> str | None:
        """Wait until the permission decision is durably visible in SQL."""
        client = exchange.client
        session_id = exchange.provider.session_id
        cancel_task = asyncio.create_task(
            exchange.controller.cancel_event.wait(),
        )
        permission_waiters: set[
            asyncio.Future[str] | asyncio.Task[bool]
        ] = {permission_future, cancel_task}
        try:
            while True:
                done, _ = await asyncio.wait(
                    permission_waiters,
                    timeout=0.5,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if cancel_task in done:
                    progress.cancelled = True
                    return None
                persisted, cancelled = await run_owned(
                    self._permission_state,
                    exchange.inputs.workspace_id,
                    exchange.inputs.user_id,
                    permission_id,
                )
                if cancelled:
                    await client.abort_session(session_id)
                    progress.cancelled = True
                    return None
                if persisted:
                    return persisted
                if permission_future in done:
                    _raise_permission_not_persisted()
        finally:
            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)

    async def _reject_disabled_permission(
        self,
        exchange: Exchange,
        progress: StreamProgress,
        permission_id: str,
    ) -> PermissionResponse:
        """Persist the safe rejection when permission UI is disabled."""
        response = await run_owned(
            self._reject_pending_permission,
            exchange.inputs.workspace_id,
            exchange.inputs.user_id,
            permission_id,
        )
        if response is None:
            _, cancelled = await run_owned(
                self._permission_state,
                exchange.inputs.workspace_id,
                exchange.inputs.user_id,
                permission_id,
            )
            progress.cancelled = cancelled
            return PermissionResponse(None)
        return PermissionResponse(response, response == "reject")

    @staticmethod
    def _permission_response_dispatchable(
        permission: AssistantPermission,
        response: str | None,
        progress: StreamProgress,
    ) -> str | None:
        """Return a durable response or record that cancellation won."""
        if response in {"once", "reject"}:
            return response
        if permission.state == "cancelled":
            progress.cancelled = True
            return None
        message = "Permission response is not durable"
        raise ConflictError(message)

    async def _prepare_permission_response(
        self,
        exchange: Exchange,
        progress: StreamProgress,
        permission: AssistantPermission,
        permission_id: str,
        mapped: AssistantEvent,
    ) -> PermissionResponse | PermissionWait:
        """Resolve an existing decision or prepare the UI wait event."""
        if permission.state == "answered":
            response = permission.response
            if response not in {"once", "reject"}:
                message = "Permission response is not durable"
                raise ConflictError(message)
            return PermissionResponse(response)
        if permission.state == "pending":
            if not _permission_ui_enabled():
                return await self._reject_disabled_permission(
                    exchange,
                    progress,
                    permission_id,
                )
            details = permission.details
            if details is None:
                message = "Stored permission details are invalid"
                raise ConflictError(message)
            return PermissionWait(
                event=AssistantEvent(
                    mapped.type,
                    details,
                    mapped.run_id,
                    mapped.message_id,
                ),
                future=exchange.controller.wait_for_permission(permission_id),
            )
        message = "Permission request is in an invalid state"
        raise ConflictError(message)

    async def _dispatch_permission_response(
        self,
        exchange: Exchange,
        progress: StreamProgress,
        permission: AssistantPermission,
        permission_id: str,
        prepared: PermissionResponse,
    ) -> AssistantEvent:
        """Dispatch one durable answer and return its status event."""
        dispatch_response = self._permission_response_dispatchable(
            permission,
            prepared.response,
            progress,
        )
        if dispatch_response is None:
            return AssistantEvent("cancelled", {"status": "cancelled"})
        try:
            await run_owned(
                self._dispatch_permission,
                exchange.inputs.workspace_id,
                exchange.inputs.user_id,
                exchange.runtime,
                PermissionDispatch(
                    provider_session_id=exchange.provider.session_id,
                    permission_id=permission_id,
                    response=dispatch_response,
                ),
            )
        except BaseException:
            await _abort_quietly(
                exchange.client,
                exchange.provider.session_id,
            )
            raise
        progress.permission_rejected = dispatch_response == "reject"
        return AssistantEvent(
            "status",
            {
                "content": (
                    "权限确认功能未开启,已拒绝高风险操作"
                    if prepared.auto_rejected
                    else "权限已响应,继续执行"
                ),
            },
        )

    async def _resolve_permission(
        self,
        exchange: Exchange,
        progress: StreamProgress,
        mapped: AssistantEvent,
    ) -> AsyncGenerator[AssistantEvent, None]:
        """Register the provider request, then dispatch its durable decision."""
        permission_id = str(mapped.payload.get("permission_id") or "")
        permission = await run_owned(
            self._persist_permission,
            exchange.inputs.workspace_id,
            exchange.inputs.user_id,
            mapped.payload,
        )
        if permission.state == "dispatched":
            return
        if permission.state == "cancelled":
            progress.cancelled = True
            yield AssistantEvent("cancelled", {"status": "cancelled"})
            return

        prepared = await self._prepare_permission_response(
            exchange,
            progress,
            permission,
            permission_id,
            mapped,
        )
        if isinstance(prepared, PermissionWait):
            yield prepared.event
            try:
                response = await self._wait_for_permission_response(
                    exchange,
                    progress,
                    permission_id,
                    prepared.future,
                )
                if response is None:
                    if progress.cancelled:
                        yield AssistantEvent(
                            "cancelled",
                            {"status": "cancelled"},
                        )
                    return
            except BaseException:
                # A revoked role, DB failure or disconnected waiter must not
                # leave a live provider task.
                await _abort_quietly(
                    exchange.client,
                    exchange.provider.session_id,
                )
                raise
            dispatch_prepared = PermissionResponse(response)
        else:
            if prepared.response is None:
                if progress.cancelled:
                    yield AssistantEvent(
                        "cancelled",
                        {"status": "cancelled"},
                    )
                return
            dispatch_prepared = prepared

        event = await self._dispatch_permission_response(
            exchange,
            progress,
            permission,
            permission_id,
            dispatch_prepared,
        )
        yield event

    async def _recover_reply(
        self,
        exchange: Exchange,
        progress: StreamProgress,
    ) -> ReplyRecovery:
        """Rebuild the answer from the durable message API after a drop."""
        messages = await exchange.client.get_messages(
            exchange.provider.session_id,
        )
        previous_ids = progress.previous_ids or set()
        text, provider_id, usage, title = _current_reply(
            messages, previous_ids, exchange.inputs.query,
        )
        return ReplyRecovery(
            text=text,
            provider_id=provider_id,
            usage=usage,
            title=title,
        )

    async def _finalize_exchange(
        self,
        exchange: Exchange,
        progress: StreamProgress,
    ) -> AsyncGenerator[AssistantEvent, None]:
        """Emit the durable final answer, the diff, and the terminal event."""
        client = exchange.client
        session_id = exchange.provider.session_id
        messages = await client.get_messages(session_id)
        final_text, provider_id, usage, title = _current_reply(
            messages,
            progress.previous_ids or set(),
            exchange.inputs.query,
        )
        if not final_text:
            if progress.permission_rejected and progress.done_seen:
                progress.cancelled = True
                yield AssistantEvent("cancelled", {"status": "cancelled"})
                return
            message = (
                "OpenCode did not confirm a completed answer for this query"
            )
            raise OpenCodeClientError(message)
        if title and not progress.emitted_title:
            yield AssistantEvent("title", {"content": title})
            progress.emitted_title = title
        suffix = _answer_suffix(final_text, progress.emitted_text)
        if suffix:
            yield AssistantEvent("token", {"content": suffix})
        if provider_id or usage:
            yield AssistantEvent(
                "usage",
                {"provider_message_id": provider_id, **usage},
            )
        diffs = await client.get_diff(session_id, provider_id)
        if diffs:
            yield AssistantEvent("file_diff", {"files": diffs})
        if exchange.controller.cancel_event.is_set():
            yield AssistantEvent("cancelled", {"status": "cancelled"})
            return
        yield AssistantEvent("done", {"status": "completed"})


async def _recovery_events(
    recovery: ReplyRecovery,
    progress: StreamProgress,
) -> AsyncGenerator[AssistantEvent, None]:
    """Emit the recovered answer parts that were not streamed yet."""
    if recovery.text and not progress.emitted_text:
        yield AssistantEvent("token", {"content": recovery.text})
        progress.emitted_text = recovery.text
    if recovery.title and not progress.emitted_title:
        yield AssistantEvent("title", {"content": recovery.title})
        progress.emitted_title = recovery.title
    if recovery.provider_id or recovery.usage:
        yield AssistantEvent(
            "usage",
            {
                "provider_message_id": recovery.provider_id,
                **recovery.usage,
            },
        )
