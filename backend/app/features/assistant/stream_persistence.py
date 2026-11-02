"""在单个 worker 中序列化流持久化; ORM 值保留在该 worker 上."""

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from types import TracebackType
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar

import anyio
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.exceptions import ConflictError, ResourceNotFoundError
from app.features.assistant import abort_repository, repository
from app.features.assistant.engines import fast_engine
from app.features.assistant.engines.base import AssistantEngine
from app.features.assistant.engines.expert_engine import ExpertEngine
from app.features.assistant.event_protocol import AssistantEvent
from app.features.workspace.permissions import (
    assert_can_write,
    assert_member,
    get_member_role,
)
from app.models.assistant import (
    AssistantConversation,
    AssistantMessage,
    AssistantRun,
)

if TYPE_CHECKING:
    from app.api.stream_credentials import StreamCredential
from app.models.workspace import Workspace

P = ParamSpec("P")
R = TypeVar("R")


def _empty_answer_parts() -> list[str]:
    """返回全新的回答缓冲区."""
    return []


def _empty_events() -> list[AssistantEvent]:
    """返回全新的事件日志."""
    return []


def _empty_metadata() -> dict[str, Any]:
    """返回全新的元数据字典."""
    return {}


@dataclass(slots=True)
class StreamProgress:
    """由单次流式运行拥有的可变检查点状态."""

    answer_parts: list[str] = field(default_factory=_empty_answer_parts)
    event_log: list[AssistantEvent] = field(default_factory=_empty_events)
    metadata: dict[str, Any] = field(default_factory=_empty_metadata)
    usage: dict[str, Any] = field(default_factory=_empty_metadata)
    event_count: int = 0
    last_commit: float = 0.0
    last_lease: float = 0.0

    @property
    def content(self) -> str:
        """返回截至目前累积的回答文本."""
        return "".join(self.answer_parts)


@dataclass(frozen=True, slots=True)
class LeasePoll:
    """流式 worker 内部的单次租约/权限轮询."""

    renew_lease: bool
    now: float
    idle: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class FinalOutcome:
    """为单次运行持久化的终态值."""

    status: str
    content: str = ""
    metadata: dict[str, Any] = field(default_factory=_empty_metadata)
    usage: dict[str, Any] = field(default_factory=_empty_metadata)
    events: list[AssistantEvent] = field(default_factory=_empty_events)
    error_code: str | None = None


_FINAL_MESSAGES = {
    "expert": "专家任务执行失败",
    "fast": "快速问答执行失败",
}


class StreamWorker:
    """在专用线程上为单个流运行所有 SQL 操作."""

    def __init__(self) -> None:
        """创建拥有 Session 的单线程执行器."""
        self.executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="assistant-db",
        )

    async def call(
        self,
        function: Callable[P, R],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> R:
        """运行单个阻塞调用并在取消返回前将其排干."""
        future = asyncio.wrap_future(
            self.executor.submit(function, *args, **kwargs),
        )
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            # 运行中的 SQL 操作无法取消. 在清理或其他操作
            # 接触同一 Session 之前必须排干该操作.
            with anyio.CancelScope(shield=True):
                while not future.done():
                    try:
                        await asyncio.shield(future)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
            if future.done() and not future.cancelled():
                future.exception()
            raise

    async def __aenter__(self) -> "StreamWorker":
        """进入 worker 上下文."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """停止执行器且不等待已取消的 SQL 调用."""
        del exc_type, exc, tb
        self.executor.shutdown(wait=False)


class StreamPersistence:
    """单次准备好的助手流的 SQL 状态."""

    def __init__(
        self,
        session: Session,
        workspace: Workspace,
        user_id: int,
        conversation: AssistantConversation,
        run: AssistantRun,
    ) -> None:
        """校验所有权并捕获持久化标识."""
        self.session = session
        self.workspace = workspace
        self.user_id = int(user_id)
        self.conversation = conversation
        self.run = run
        if (
            run.conversation_id != conversation.id
            or conversation.workspace_id != workspace.id
            or conversation.user_id != self.user_id
        ):
            message = "Assistant run not found"
            raise ResourceNotFoundError(message)
        assert_member(session, int(workspace.id), self.user_id)
        if conversation.mode == "expert":
            assert_can_write(session, int(workspace.id), self.user_id)
        self.run_id = int(run.id)
        self.credential: StreamCredential | None = None
        assistant_message = session.get(AssistantMessage, run.assistant_message_id)
        if assistant_message is None:
            error = "Assistant message not found"
            raise ResourceNotFoundError(error)
        self.message: AssistantMessage = assistant_message
        self.message_id = int(assistant_message.id)
        self.workspace_id = int(workspace.id)
        self.mode = str(conversation.mode)
        user_message = session.get(AssistantMessage, run.user_message_id)
        if user_message is None:
            error = "Assistant user message not found"
            raise ResourceNotFoundError(error)
        self.query = str(user_message.content)

    def initialize(self, now: float) -> tuple[int, int, int, str, str]:
        """获取运行租约并返回不可变的流输入."""
        if self.credential is not None:
            self.credential.validate(self.session, self.user_id)
        active = repository.touch_run_lease(self.session, self.run_id, now)
        self.session.commit()
        if not active:
            message = "任务已结束,不能继续执行"
            raise ConflictError(message)
        return self.run_id, self.message_id, self.workspace_id, self.mode, self.query

    def poll(self, request: LeasePoll) -> tuple[bool, bool]:
        """刷新租约并读取终态/取消状态."""
        if request.idle:
            # 在等待更多提供商事件前刷新检查点.
            # 空闲但健康的流不得触碰事务空闲写入超时上限.
            self.session.commit()
        if self.credential is not None:
            self.credential.validate(self.session, self.user_id)
        role = get_member_role(self.session, self.workspace_id, self.user_id)
        revoked = role is None or (
            self.mode == "expert" and role not in {"editor", "admin"}
        )
        if request.renew_lease:
            active = repository.touch_run_lease(
                self.session, self.run_id, request.now,
            )
            self.session.commit()
            if not active:
                return True, True
        row = (
            self.session.query(
                AssistantRun.status,
                AssistantRun.cancel_requested,
            )
            .filter(AssistantRun.id == self.run_id)
            .first()
        )
        return (
            row is None or row[0] in repository.TERMINAL_RUN_STATUSES,
            revoked or bool(row and row[1]),
        )

    def checkpoint(
        self,
        progress: StreamProgress,
        *,
        commit: bool,
        permission_boundary: bool = False,
    ) -> bool:
        """持久化流进度且不改变权限决策."""
        if self.session.get_bind().dialect.name == "postgresql":
            # 挂起或崩溃的 API worker 不得在检查点之间永久持有
            # 消息/运行写锁. 事务本地配置不得泄漏给
            # 其他连接池调用方.
            self.session.execute(
                text("SET LOCAL idle_in_transaction_session_timeout = '5s'"),
            )
        if permission_boundary:
            run = repository.lock_run(self.session, self.run_id)
            if run is None or run.status in repository.TERMINAL_RUN_STATUSES:
                self.session.commit()
                return False
        self.message.content = progress.content
        self.message.metadata_dict = dict(progress.metadata)
        self.message.usage = dict(progress.usage)
        self.session.flush()
        if commit:
            self.session.commit()
        return True

    def finish(self, outcome: FinalOutcome) -> str | None:
        """在刷新持久化状态后应用终态转换."""
        # 从失败的 flush/commit 中恢复, 并刷新其他 API worker
        # 可能修改的状态, 随后决定终态转换.
        self.session.rollback()
        run = repository.lock_run(self.session, self.run_id)
        if run is None:
            self.session.commit()
            return None
        if run.status in repository.TERMINAL_RUN_STATUSES:
            if run.status in {"failed", "cancelled"}:
                abort_repository.stage(self.session, run)
            result = run.status
            self.session.commit()
            return result
        status = outcome.status
        if run.cancel_requested:
            status = "cancelled"
        if status in {"failed", "cancelled"}:
            abort_repository.stage(self.session, run)
        self.message.content = outcome.content
        self.message.status = status
        self.message.metadata_dict = dict(outcome.metadata)
        self.message.usage = dict(outcome.usage)
        run.metadata_dict = repository.serialize_events_metadata(outcome.events)
        repository.update_run_status(
            self.session,
            run,
            status,
            error_code=outcome.error_code,
            error_message=(
                _FINAL_MESSAGES["expert"]
                if status == "failed" and self.mode == "expert"
                else _FINAL_MESSAGES["fast"]
                if status == "failed"
                else None
            ),
        )
        self.session.commit()
        return status

    def engine(self) -> AssistantEngine:
        """构建对话模式所选用的引擎."""
        if self.mode == "fast":
            # 调用时读取属性, 便于测试替换引擎.
            return fast_engine.FastEngine()
        return ExpertEngine(
            session=self.session,
            conversation=self.conversation,
            run=self.run,
        )
