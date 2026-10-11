"""Serialize lifecycle/configuration across commits on a pinned PG connection."""
from contextlib import contextmanager
from functools import wraps
import hashlib
import threading

from sqlalchemy import text
from loguru import logger

from app.exceptions import ConflictError

_local = threading.local()
_guard = threading.Lock()
_sqlite_locks: dict[tuple[int, int, int], tuple[threading.RLock, int]] = {}


def _invalidate(connection):
    try:
        connection.invalidate()
    except Exception as exc:
        logger.error('Runtime lock connection invalidation failed: error_type={}', type(exc).__name__)


@contextmanager
def execution(session, workspace_id, user_id):
    bind = session.get_bind()
    key = (id(bind.engine), int(workspace_id), int(user_id))
    active: set[tuple[int, int, int]] = getattr(_local, 'active', set())
    if key in active:
        yield
        return
    _local.active = active
    if bind.dialect.name == 'sqlite':
        # Test seam only: deployment startup requires PostgreSQL.
        with _guard:
            lock, count = _sqlite_locks.get(key, (threading.RLock(), 0))
            _sqlite_locks[key] = (lock, count+1)
        try:
            with lock:
                active.add(key)
                try: yield
                finally: active.remove(key)
        finally:
            with _guard:
                lock, count = _sqlite_locks[key]
                if count == 1: del _sqlite_locks[key]
                else: _sqlite_locks[key] = (lock, count-1)
        return
    if bind.dialect.name != 'postgresql':
        raise RuntimeError('Runtime lifecycle requires PostgreSQL')
    lock_id = int.from_bytes(hashlib.sha256(f'Runtime.lifecycle:{workspace_id}:{user_id}'.encode()).digest()[:8], 'big', signed=True)
    connection = bind.engine.connect()
    acquired = False
    try:
        acquired = connection.scalar(text('SELECT pg_try_advisory_lock(:key)'), {'key':lock_id})
        connection.commit()
        if not acquired:
            raise ConflictError('Another Runtime lifecycle operation is in progress')
        active.add(key)
        try: yield
        finally: active.remove(key)
    finally:
        try:
            if acquired:
                connection.execute(text('SELECT pg_advisory_unlock(:key)'), {'key':lock_id})
                connection.commit()
        except Exception as exc:
            logger.error('Runtime lock release failed: error_type={}', type(exc).__name__)
            _invalidate(connection)
        finally:
            try:
                connection.close()
            except Exception as exc:
                logger.error('Runtime lock connection close failed: error_type={}', type(exc).__name__)
                _invalidate(connection)


def serialized(function):
    @wraps(function)
    def wrapper(session, workspace, user_id=None, *args, **kwargs):
        if user_id is None:
            return function(session, workspace, user_id, *args, **kwargs)
        with execution(session, workspace.id, user_id):
            return function(session, workspace, user_id, *args, **kwargs)
    return wrapper
