"""Serialize stream persistence in one worker; ORM values stay on that worker."""

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
    """Return a fresh answer buffer."""
    return []


def _empty_events() -> list[AssistantEvent]:
    """Return a fresh event log."""
    return []


def _empty_metadata() -> dict[str, Any]:
    """Return a fresh metadata mapping."""
    return {}


@dataclass(slots=True)
class StreamProgress:
    """Mutable checkpoint state owned by one streaming run."""

    answer_parts: list[str] = field(default_factory=_empty_answer_parts)
    event_log: list[AssistantEvent] = field(default_factory=_empty_events)
    metadata: dict[str, Any] = field(default_factory=_empty_metadata)
    usage: dict[str, Any] = field(default_factory=_empty_metadata)
    event_count: int = 0
    last_commit: float = 0.0
    last_lease: float = 0.0

    @property
    def content(self) -> str:
        """Return the answer text accumulated so far."""
        return "".join(self.answer_parts)


@dataclass(frozen=True, slots=True)
class LeasePoll:
    """One lease/permission poll inside the streaming worker."""

    renew_lease: bool
    now: float
    idle: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class FinalOutcome:
    """Terminal values persisted for one run."""

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
    """Run every SQL operation for one stream on a dedicated thread."""

    def __init__(self) -> None:
        """Create the single-thread executor that owns the Session."""
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
        """Run one blocking call and drain it before cancellation returns."""
        future = asyncio.wrap_future(
            self.executor.submit(function, *args, **kwargs),
        )
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            # A running SQL operation cannot be cancelled. Drain it before
            # cleanup or another operation can touch the same Session.
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
        """Enter the worker context."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Stop the executor without waiting for a cancelled SQL call."""
        del exc_type, exc, tb
        self.executor.shutdown(wait=False)


class StreamPersistence:
    """SQL state for one prepared assistant stream."""

    def __init__(
        self,
        session: Session,
        workspace: Workspace,
        user_id: int,
        conversation: AssistantConversation,
        run: AssistantRun,
    ) -> None:
        """Validate ownership and capture the durable identities."""
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
        """Acquire the run lease and return the immutable stream inputs."""
        if self.credential is not None:
            self.credential.validate(self.session, self.user_id)
        active = repository.touch_run_lease(self.session, self.run_id, now)
        self.session.commit()
        if not active:
            message = "任务已结束,不能继续执行"
            raise ConflictError(message)
        return self.run_id, self.message_id, self.workspace_id, self.mode, self.query

    def poll(self, request: LeasePoll) -> tuple[bool, bool]:
        """Refresh the lease and read terminal/cancellation state."""
        if request.idle:
            # Flush checkpoints before waiting for more provider events. Quiet
            # healthy streams must not hit the idle-write-transaction bound.
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
        """Persist stream progress without mutating permission decisions."""
        if self.session.get_bind().dialect.name == "postgresql":
            # A suspended/dead API worker must not retain message/run write
            # locks forever between streaming checkpoints. Transaction-local
            # configuration cannot leak into other pooled-session callers.
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
        """Apply the terminal transition after refreshing durable state."""
        # Recover from a failed flush/commit and refresh any state changed by
        # another API worker before deciding the terminal transition.
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
        """Build the engine selected by the conversation mode."""
        if self.mode == "fast":
            # Read the attribute at call time so tests can swap the engine.
            return fast_engine.FastEngine()
        return ExpertEngine(
            session=self.session,
            conversation=self.conversation,
            run=self.run,
        )
