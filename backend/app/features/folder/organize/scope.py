"""Immutable task identity explicitly bound to every agent tool invocation."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from inspect import signature
from typing import Callable

from langchain.tools import tool
from sqlalchemy.orm import Session

from app.exceptions import PermissionDeniedError


@dataclass(frozen=True)
class OrganizeScope:
    workspace_id: int
    user_id: int
    session_factory: Callable[[], Session]


_current_scope: ContextVar[OrganizeScope | None] = ContextVar("organize_scope", default=None)


def current_scope() -> OrganizeScope | None:
    return _current_scope.get()


@contextmanager
def organize_scope(scope: OrganizeScope):
    if scope.workspace_id <= 0 or scope.user_id <= 0:
        raise PermissionDeniedError("Organize task requires a valid user and workspace")
    token = _current_scope.set(scope)
    try:
        yield
    finally:
        _current_scope.reset(token)


def bind_tools(source_tools, scope: OrganizeScope):
    """Bind identity in a closure so parallel LangGraph tools cannot lose scope."""
    def bind(source):
        function = getattr(source, "func", None) or source.__wrapped__
        parameters = signature(function)

        @wraps(function)
        def invoke(*args, **kwargs):
            values = parameters.bind(*args, **kwargs).arguments
            if "workspace_id" in values and values["workspace_id"] != scope.workspace_id:
                raise PermissionDeniedError("Organize tool cannot access another workspace")
            with organize_scope(scope):
                return function(*args, **kwargs)
        return tool(invoke)
    return [bind(source) for source in source_tools]
