"""Immutable task identity explicitly bound to every agent tool invocation."""

from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from inspect import signature
from typing import cast

from langchain.tools import BaseTool, tool
from sqlalchemy.orm import Session

from app.exceptions import PermissionDeniedError
from app.features.folder.organize.tasks import assert_task_claim
from app.features.workspace.permissions import assert_can_write
from app.infra.extensions import SessionFactory


@dataclass(frozen=True)
class OrganizeScope:
    """Workspace, user and task identity bound to one organizer run."""

    workspace_id: int
    user_id: int
    session_factory: SessionFactory
    task_id: str | None = None
    execution_token: str | None = None


_current_scope: ContextVar[OrganizeScope | None] = ContextVar(
    "organize_scope",
    default=None,
)


def current_scope() -> OrganizeScope | None:
    """Return the scope bound to the current context, if any."""
    return _current_scope.get()


@contextmanager
def organize_scope(scope: OrganizeScope) -> Generator[None, None, None]:
    """Bind the scope for the duration of the block, restoring it afterwards."""
    if scope.workspace_id <= 0 or scope.user_id <= 0:
        message = "Organize task requires a valid user and workspace"
        raise PermissionDeniedError(message)
    token = _current_scope.set(scope)
    try:
        yield
    finally:
        _current_scope.reset(token)


def _tool_callable(source: BaseTool) -> Callable[..., object]:
    """Return the python callable wrapped by a LangChain tool.

    LangChain stores it on ``func`` for structured tools and on ``__wrapped__``
    for plain function wrappers, so the lookup stays behind a typed boundary.
    """
    function = getattr(source, "func", None) or getattr(source, "__wrapped__", None)
    return cast("Callable[..., object]", function)


def _bind_tool(source: BaseTool, scope: OrganizeScope) -> BaseTool:
    """Wrap one tool so it always runs inside the bound identity."""
    function = _tool_callable(source)
    parameters = signature(function)

    @wraps(function)
    def invoke(*args: object, **kwargs: object) -> object:
        values = parameters.bind(*args, **kwargs).arguments
        if "workspace_id" in values and values["workspace_id"] != scope.workspace_id:
            message = "Organize tool cannot access another workspace"
            raise PermissionDeniedError(message)
        with organize_scope(scope):
            return function(*args, **kwargs)

    return tool(invoke)


def bind_tools(
    source_tools: Sequence[BaseTool],
    scope: OrganizeScope,
) -> list[BaseTool]:
    """Bind identity in a closure so parallel LangGraph tools cannot lose scope."""
    return [_bind_tool(source, scope) for source in source_tools]


def authorize_session(session: Session, scope: OrganizeScope) -> None:
    """Re-verify the durable task claim and write permission for this session."""
    if scope.task_id is not None:
        assert_task_claim(
            session,
            scope.task_id,
            scope.execution_token,
            scope.workspace_id,
            scope.user_id,
        )
    assert_can_write(session, scope.workspace_id, scope.user_id)
