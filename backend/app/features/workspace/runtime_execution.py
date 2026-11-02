"""在固定的 PostgreSQL 连接上跨事务提交串行化生命周期与配置变更."""

import hashlib
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from functools import wraps
from typing import Concatenate, ParamSpec, TypeAlias, TypeVar

from loguru import logger
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

from app.exceptions import ConflictError
from app.models.workspace import Workspace

_P = ParamSpec("_P")
_R = TypeVar("_R")
_ExecutionKey: TypeAlias = tuple[int, int, int]


class _ExecutionState(threading.local):
    """已持有咨询锁的主键键名的线程本地集合."""

    active: set[_ExecutionKey] | None = None


_state = _ExecutionState()
_guard = threading.Lock()
_sqlite_locks: dict[_ExecutionKey, tuple[threading.RLock, int]] = {}


def _invalidate(connection: Connection) -> None:
    try:
        connection.invalidate()
    except Exception as exc:
        logger.error(
            "Runtime lock connection invalidation failed: error_type={}",
            type(exc).__name__,
        )


def _active_keys() -> set[_ExecutionKey]:
    active = _state.active
    if active is None:
        active = set[_ExecutionKey]()
        _state.active = active
    return active


@contextmanager
def _sqlite_execution(
    key: _ExecutionKey,
    active: set[_ExecutionKey],
) -> Generator[None, None, None]:
    # 仅用于单元测试缝隙: 生产部署启动强制要求 PostgreSQL。
    with _guard:
        lock, count = _sqlite_locks.get(key, (threading.RLock(), 0))
        _sqlite_locks[key] = (lock, count + 1)
    try:
        with lock:
            active.add(key)
            try:
                yield
            finally:
                active.remove(key)
    finally:
        with _guard:
            held_lock, held_count = _sqlite_locks[key]
            if held_count == 1:
                del _sqlite_locks[key]
            else:
                _sqlite_locks[key] = (held_lock, held_count - 1)


def _release_connection(
    connection: Connection,
    lock_id: int,
    *,
    acquired: bool,
) -> None:
    try:
        if acquired:
            connection.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": lock_id},
            )
            connection.commit()
    except Exception as exc:
        logger.error("Runtime lock release failed: error_type={}", type(exc).__name__)
        _invalidate(connection)
    finally:
        try:
            connection.close()
        except Exception as exc:
            logger.error(
                "Runtime lock connection close failed: error_type={}",
                type(exc).__name__,
            )
            _invalidate(connection)


@contextmanager
def _postgres_execution(
    engine: Engine,
    key: _ExecutionKey,
    active: set[_ExecutionKey],
    lock_id: int,
) -> Generator[None, None, None]:
    connection = engine.connect()
    acquired = False
    try:
        acquired = bool(connection.scalar(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_id},
        ))
        connection.commit()
        if not acquired:
            message = "Another Runtime lifecycle operation is in progress"
            raise ConflictError(message)
        active.add(key)
        try:
            yield
        finally:
            active.remove(key)
    finally:
        _release_connection(connection, lock_id, acquired=acquired)


@contextmanager
def execution(
    session: Session,
    workspace_id: int,
    user_id: int,
) -> Generator[None, None, None]:
    """为该工作空间成员持有 Runtime 生命周期排他锁."""
    bind = session.get_bind()
    engine = bind if isinstance(bind, Engine) else bind.engine
    key = (id(engine), int(workspace_id), int(user_id))
    active = _active_keys()
    if key in active:
        yield
        return
    if bind.dialect.name == "sqlite":
        with _sqlite_execution(key, active):
            yield
        return
    if bind.dialect.name != "postgresql":
        message = "Runtime lifecycle requires PostgreSQL"
        raise RuntimeError(message)
    lock_id = int.from_bytes(
        hashlib.sha256(
            f"Runtime.lifecycle:{workspace_id}:{user_id}".encode(),
        ).digest()[:8],
        "big",
        signed=True,
    )
    with _postgres_execution(engine, key, active, lock_id):
        yield


def serialized(
    function: Callable[Concatenate[Session, Workspace, int, _P], _R],
) -> Callable[Concatenate[Session, Workspace, int, _P], _R]:
    """在持有 Runtime 咨询锁的同时执行生命周期函数."""

    @wraps(function)
    def wrapper(
        session: Session,
        workspace: Workspace,
        user_id: int,
        *args: _P.args,
        **kwargs: _P.kwargs,
    ) -> _R:
        with execution(session, int(workspace.id), user_id):
            return function(session, workspace, user_id, *args, **kwargs)

    return wrapper
