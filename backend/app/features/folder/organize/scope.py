"""显式绑定至每次 Agent 工具调用的不可变任务身份上下文."""

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
    """绑定至单次整理运行的工作空间、用户与任务身份."""

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
    """返回当前上下文绑定的作用域范围 (若存在)."""
    return _current_scope.get()


@contextmanager
def organize_scope(scope: OrganizeScope) -> Generator[None, None, None]:
    """在代码块执行期间绑定作用域, 退出时恢复原有上下文."""
    if scope.workspace_id <= 0 or scope.user_id <= 0:
        message = "Organize task requires a valid user and workspace"
        raise PermissionDeniedError(message)
    token = _current_scope.set(scope)
    try:
        yield
    finally:
        _current_scope.reset(token)


def _tool_callable(source: BaseTool) -> Callable[..., object]:
    """返回 LangChain 工具底层包装的 Python 可调用对象.

    LangChain 结构化工具存储在 ``func``, 普通函数包装器存储在
    ``__wrapped__``, 此处通过类型化边界进行安全查找。
    """
    function = getattr(source, "func", None) or getattr(source, "__wrapped__", None)
    return cast("Callable[..., object]", function)


def _bind_tool(source: BaseTool, scope: OrganizeScope) -> BaseTool:
    """包装单个工具使其始终在绑定的身份作用域内运行."""
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
    """在闭包中绑定身份, 避免 LangGraph 并行工具调用丢失作用域."""
    return [_bind_tool(source, scope) for source in source_tools]


def authorize_session(session: Session, scope: OrganizeScope) -> None:
    """重新校验当前会话的持久任务认领权与工作空间写权限."""
    if scope.task_id is not None:
        assert_task_claim(
            session,
            scope.task_id,
            scope.execution_token,
            scope.workspace_id,
            scope.user_id,
        )
    assert_can_write(session, scope.workspace_id, scope.user_id)
