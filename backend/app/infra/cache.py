"""Spring 风格 Redis 缓存装饰器, 供 service 层复用.

三种装饰器对应 Spring Boot 注解语义:

    @cacheable  — 先读缓存; 未命中则执行函数并写入(Spring @Cacheable)
    @cache_evict — 执行函数后清除匹配键(Spring @CacheEvict)
    @cache_put  — 始终执行函数并回写缓存(Spring @CachePut)

读/写失败只记日志, 不阻断业务; None 默认不缓存, 避免穿透。
"""

import functools
import inspect
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Generic, ParamSpec, Protocol, TypeVar, cast, overload

from loguru import logger
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.infra.extensions import redis_client
from app.infra.owned_work import run_owned, run_owned_persistent
from app.models.cache_revision import SqlCacheRevision

_P = ParamSpec("_P")
_R = TypeVar("_R")
_R_co = TypeVar("_R_co", covariant=True)

_GENERATION_LENGTH = 32
_EVICTION_BATCH_SIZE = 128
_CACHE_MISS = object()


class _SQLiteDriver(Protocol):
    @property
    def in_transaction(self) -> bool: ...


# ---------------------------------------------------------------------------
# 缓存键构造
# ---------------------------------------------------------------------------

def _build_cache_key(
    prefix: str,
    args: tuple[object, ...],
    kwargs: dict[str, object],
    key: Callable[_P, object] | None = None,
) -> str:
    """拼 Redis 键: ``prefix`` + 自定义 key 回调或全部位置参数.

    示例(prefix="user:profile"):
        get_user(2)            → "user:profile:2"
        key=lambda id, **_: id → "user:profile:2"
    """
    if key is not None:
        # ParamSpec cannot express a signature reconstructed from stored args.
        suffix = str(cast("Callable[..., object]", key)(*args, **kwargs))
        return f"{prefix}:{suffix}"

    parts = [str(value) for value in args]
    if kwargs:
        parts.extend(f"{name}={value}" for name, value in sorted(kwargs.items()))
    return f"{prefix}:{':'.join(parts)}" if parts else prefix


# ---------------------------------------------------------------------------
# @cacheable — 读穿缓存
# ---------------------------------------------------------------------------

def sql_cache_revision(session: Session, scope: str) -> int:
    """Read the SQL revision for a cache scope, defaulting missing scopes to zero."""
    return (
        session.scalar(
            select(SqlCacheRevision.revision).where(SqlCacheRevision.scope == scope),
        )
        or 0
    )


def sql_cache_allowed(session: Session, *args: object, **kwargs: object) -> bool:
    """Keep shared SQL caches outside transactions which may roll back writes."""
    del args, kwargs
    if session.new or session.dirty or session.deleted:
        return False
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        # Flush/direct SQL can empty ORM dirty state while writes remain live.
        return session.scalar(text("SELECT pg_current_xact_id_if_assigned()")) is None
    if dialect == "sqlite":
        driver = cast(
            "_SQLiteDriver", session.connection().connection.driver_connection,
        )
        return not driver.in_transaction
    return not session.in_transaction()


def invalidate_cache_generation(scope: str) -> None:
    """Rotate a cache namespace before removing old physical keys."""
    try:
        redis_client.set(scope, uuid.uuid4().hex)
    except Exception as exc:
        # Redis adapters may fail arbitrarily; invalidation must not undo a write.
        logger.warning(
            "[cache] generation invalidation failed: error_type={}",
            type(exc).__name__,
        )


class CachedFunction(Protocol[_P, _R_co]):
    """装饰后的可调用对象; 经 ``cache_prefix`` 暴露缓存前缀供外部内省."""

    cache_prefix: str

    def __call__(self, *args: _P.args, **kwargs: _P.kwargs) -> _R_co:
        """Call the decorated business function with its original signature."""
        ...


class _CacheableDecorator(Protocol):
    """A cache decorator without signature-constraining callbacks."""

    def __call__(self, func: Callable[_P, _R]) -> CachedFunction[_P, _R]: ...


class _FunctionDecorator(Protocol):
    """A non-cacheable decorator without signature-constraining callbacks."""

    def __call__(self, func: Callable[_P, _R]) -> Callable[_P, _R]: ...


def _generation_cache_key(cache_key: str, scope: str) -> str:
    generation = redis_client.get(scope)
    if generation is None:
        candidate = uuid.uuid4().hex
        if redis_client.set(scope, candidate, nx=True):
            generation = candidate
        else:
            generation = redis_client.get(scope)
    if isinstance(generation, bytes):
        generation = generation.decode("ascii")
    if (
        not isinstance(generation, str)
        or len(generation) != _GENERATION_LENGTH
        or any(char not in "0123456789abcdef" for char in generation)
    ):
        message = "Invalid cache generation"
        raise ValueError(message)
    return f"{cache_key}:generation:{generation}"


@dataclass(frozen=True, slots=True, kw_only=True)
class CacheableParams:
    """Configure a read-through cache without changing the wrapped signature."""

    prefix: str
    expire: int = 3600
    cache_none: bool = False


class _Cacheable(Generic[_P]):
    def __init__(
        self,
        params: CacheableParams,
        *,
        key: Callable[_P, object] | None,
        generation_key: Callable[_P, str] | None,
        cache_allowed: Callable[_P, bool] | None,
    ) -> None:
        self.params = params
        self.key = key
        self.generation_key = generation_key
        self.cache_allowed = cache_allowed

    def _read(
        self, cache_key: str, args: tuple[object, ...], kwargs: dict[str, object],
    ) -> tuple[str | None, object]:
        try:
            if self.generation_key is not None:
                generation_key = cast("Callable[..., str]", self.generation_key)
                cache_key = _generation_cache_key(
                    cache_key, generation_key(*args, **kwargs),
                )
            cached = redis_client.get(cache_key)
            if cached is not None:
                return cache_key, json.loads(cached)
        except Exception as exc:
            # Generation callbacks, Redis and JSON can fail; retain SQL fallback.
            logger.warning("[cache] read failed: error_type={}", type(exc).__name__)
            if self.generation_key is not None:
                return None, _CACHE_MISS
        return cache_key, _CACHE_MISS

    async def _read_async(
        self, cache_key: str, args: tuple[object, ...], kwargs: dict[str, object],
    ) -> tuple[str | None, object]:
        try:
            if self.generation_key is not None:
                generation_key = cast("Callable[..., str]", self.generation_key)
                cache_key = await run_owned(
                    _generation_cache_key, cache_key, generation_key(*args, **kwargs),
                )
            cached = await run_owned(redis_client.get, cache_key)
            if cached is not None:
                return cache_key, json.loads(cached)
        except Exception as exc:
            # Preserve fallback without catching BaseException/cancellation.
            logger.warning("[cache] read failed: error_type={}", type(exc).__name__)
            if self.generation_key is not None:
                return None, _CACHE_MISS
        return cache_key, _CACHE_MISS

    def _wrap_sync(self, func: Callable[_P, _R]) -> Callable[_P, _R]:
        @functools.wraps(func)
        def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            allowed = self.cache_allowed
            if allowed is not None and not allowed(*args, **kwargs):
                return func(*args, **kwargs)
            cache_key, cached = self._read(
                _build_cache_key(self.params.prefix, args, kwargs, self.key),
                args,
                kwargs,
            )
            if cached is not _CACHE_MISS:
                return cast("_R", cached)
            result = func(*args, **kwargs)
            if allowed is not None and not allowed(*args, **kwargs):
                return result
            if cache_key is not None and (
                result is not None or self.params.cache_none
            ):
                self._write(cache_key, result)
            return result

        return wrapper

    def _write(self, cache_key: str, result: object) -> None:
        try:
            redis_client.setex(
                cache_key,
                self.params.expire,
                json.dumps(result, ensure_ascii=False, default=str),
            )
        except Exception as exc:
            # Serialization and Redis failures must not replace business results.
            logger.warning("[cache] write failed: error_type={}", type(exc).__name__)

    def _wrap_async(self, func: Callable[_P, _R]) -> Callable[_P, _R]:
        awaited = cast("Callable[_P, Awaitable[_R]]", func)

        @functools.wraps(func)
        async def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            allowed = self.cache_allowed
            if allowed is not None and not await run_owned(allowed, *args, **kwargs):
                return await awaited(*args, **kwargs)
            cache_key, cached = await self._read_async(
                _build_cache_key(self.params.prefix, args, kwargs, self.key),
                args,
                kwargs,
            )
            if cached is not _CACHE_MISS:
                return cast("_R", cached)
            result = await awaited(*args, **kwargs)
            if allowed is not None and not await run_owned(allowed, *args, **kwargs):
                return result
            if cache_key is not None and (
                result is not None or self.params.cache_none
            ):
                await self._write_async(cache_key, result)
            return result

        return cast("Callable[_P, _R]", wrapper)

    async def _write_async(self, cache_key: str, result: object) -> None:
        try:
            await run_owned(
                redis_client.setex,
                cache_key,
                self.params.expire,
                json.dumps(result, ensure_ascii=False, default=str),
            )
        except Exception as exc:
            # Keep serialization on the loop and drain owned Redis I/O on cancel.
            logger.warning("[cache] write failed: error_type={}", type(exc).__name__)

    def __call__(self, func: Callable[_P, _R]) -> CachedFunction[_P, _R]:
        chosen = (
            self._wrap_async(func)
            if inspect.iscoroutinefunction(func)
            else self._wrap_sync(func)
        )
        # The actual wrapper is a function, so assigning its attribute is supported.
        cached = cast("CachedFunction[_P, _R]", chosen)
        cached.cache_prefix = self.params.prefix
        return cached


@overload
def cacheable(
    params: CacheableParams,
    *,
    key: None = None,
    generation_key: None = None,
    cache_allowed: None = None,
) -> _CacheableDecorator: ...


@overload
def cacheable(
    params: CacheableParams,
    *,
    key: Callable[_P, object],
    generation_key: Callable[_P, str] | None = None,
    cache_allowed: Callable[_P, bool] | None = None,
) -> _Cacheable[_P]: ...


@overload
def cacheable(
    params: CacheableParams,
    *,
    key: None = None,
    generation_key: Callable[_P, str],
    cache_allowed: Callable[_P, bool] | None = None,
) -> _Cacheable[_P]: ...


@overload
def cacheable(
    params: CacheableParams,
    *,
    key: None = None,
    generation_key: None = None,
    cache_allowed: Callable[_P, bool],
) -> _Cacheable[_P]: ...


def cacheable(
    params: CacheableParams,
    *,
    key: Callable[..., object] | None = None,
    generation_key: Callable[..., str] | None = None,
    cache_allowed: Callable[..., bool] | None = None,
) -> _CacheableDecorator | _Cacheable[...]:
    """先查 Redis, 未命中再执行函数并写入.

    CacheableParams 保留键前缀、TTL 和 None 策略。
    key、generation_key 和 cache_allowed 使用被装饰函数的参数。
    """
    return _Cacheable(
        params, key=key, generation_key=generation_key, cache_allowed=cache_allowed,
    )


# ---------------------------------------------------------------------------
# @cache_evict — 写后失效
# ---------------------------------------------------------------------------

class _CacheEvict(Generic[_P]):
    def __init__(
        self,
        prefix: str,
        key: Callable[_P, object] | None,
        *,
        all_entries: bool,
        before_invocation: bool,
    ) -> None:
        self.prefix = prefix
        self.key = key
        self.all_entries = all_entries
        self.before_invocation = before_invocation

    def __call__(self, func: Callable[_P, _R]) -> Callable[_P, _R]:
        def do_evict(args: tuple[object, ...], kwargs: dict[str, object]) -> None:
            try:
                if self.all_entries:
                    _evict_pattern(self.prefix)
                else:
                    cache_key = _build_cache_key(self.prefix, args, kwargs, self.key)
                    redis_client.delete(cache_key)
            except Exception as exc:
                # Arbitrary key callbacks/adapters must not replace the result.
                logger.warning(
                    "[cache] evict failed: error_type={}", type(exc).__name__,
                )

        @functools.wraps(func)
        def sync_wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            if self.before_invocation:
                do_evict(args, kwargs)
            result = func(*args, **kwargs)
            if not self.before_invocation:
                do_evict(args, kwargs)
            return result

        @functools.wraps(func)
        async def async_wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            awaited = cast("Callable[_P, Awaitable[_R]]", func)
            if self.before_invocation:
                await run_owned(do_evict, args, kwargs)
            result = await awaited(*args, **kwargs)
            if not self.before_invocation:
                await run_owned_persistent(do_evict, args, kwargs)
            return result

        chosen = async_wrapper if inspect.iscoroutinefunction(func) else sync_wrapper
        return cast("Callable[_P, _R]", chosen)


@overload
def cache_evict(
    prefix: str,
    key: None = None,
    *,
    all_entries: bool = False,
    before_invocation: bool = False,
) -> _FunctionDecorator: ...


@overload
def cache_evict(
    prefix: str,
    key: Callable[_P, object],
    *,
    all_entries: bool = False,
    before_invocation: bool = False,
) -> _CacheEvict[_P]: ...


def cache_evict(
    prefix: str,
    key: Callable[..., object] | None = None,
    *,
    all_entries: bool = False,
    before_invocation: bool = False,
) -> _FunctionDecorator | _CacheEvict[...]:
    """在被装饰函数执行前/后清除缓存.

    :param prefix: 键前缀
    :param key: 推导待删单键的回调
    :param all_entries: True 时按 ``prefix:*`` 批量删(Spring allEntries)
    :param before_invocation: True 时在函数执行前删除(Spring beforeInvocation)
    """
    return _CacheEvict(
        prefix, key, all_entries=all_entries, before_invocation=before_invocation,
    )


# ---------------------------------------------------------------------------
# @cache_put — 强制回写
# ---------------------------------------------------------------------------

class _CachePut(Generic[_P]):
    def __init__(
        self,
        prefix: str,
        expire: int,
        key: Callable[_P, object] | None,
        *,
        cache_none: bool,
    ) -> None:
        self.prefix = prefix
        self.expire = expire
        self.cache_none = cache_none
        self.key = key

    def __call__(self, func: Callable[_P, _R]) -> Callable[_P, _R]:
        @functools.wraps(func)
        def sync_wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            result = func(*args, **kwargs)
            self._put_result(args, kwargs, result)
            return result

        @functools.wraps(func)
        async def async_wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            awaited = cast("Callable[_P, Awaitable[_R]]", func)
            result = await awaited(*args, **kwargs)
            await run_owned_persistent(
                self._put_result, args, kwargs, result,
            )
            return result

        chosen = async_wrapper if inspect.iscoroutinefunction(func) else sync_wrapper
        return cast("Callable[_P, _R]", chosen)

    def _put_result(
        self, args: tuple[object, ...], kwargs: dict[str, object], result: object,
    ) -> None:
        """写入单条缓存; None 且 cache_none=False 时跳过."""
        if result is None and not self.cache_none:
            return
        cache_key = _build_cache_key(self.prefix, args, kwargs, self.key)
        try:
            redis_client.setex(
                cache_key,
                self.expire,
                json.dumps(result, ensure_ascii=False, default=str),
            )
        except Exception as exc:
            # Preserve the accepted business result on serialization/Redis errors.
            logger.warning("[cache] put failed: error_type={}", type(exc).__name__)


@overload
def cache_put(
    prefix: str,
    expire: int = 3600,
    *,
    cache_none: bool = False,
    key: None = None,
) -> _FunctionDecorator: ...


@overload
def cache_put(
    prefix: str,
    expire: int = 3600,
    *,
    cache_none: bool = False,
    key: Callable[_P, object],
) -> _CachePut[_P]: ...


def cache_put(
    prefix: str,
    expire: int = 3600,
    *,
    cache_none: bool = False,
    key: Callable[..., object] | None = None,
) -> _FunctionDecorator | _CachePut[...]:
    """始终执行函数并用结果覆盖缓存(从不先读缓存)."""
    return _CachePut(prefix, expire, key, cache_none=cache_none)


# ---------------------------------------------------------------------------
# 手动清除
# ---------------------------------------------------------------------------

def evict_cache(prefix: str, *key_parts: object) -> None:
    """手动删除单键: ``evict_cache("user:profile", user_id)``."""
    cache_key = (
        f"{prefix}:{':'.join(str(part) for part in key_parts)}" if key_parts else prefix
    )
    try:
        redis_client.delete(cache_key)
    except Exception as exc:
        # Manual invalidation is best effort after accepted business writes.
        logger.warning("[cache] manual evict failed: error_type={}", type(exc).__name__)


def _evict_pattern(prefix: str) -> int:
    """分批删除匹配键, 返回成功提交删除请求的键数."""
    completed = 0
    batch: list[str] = []
    try:
        for cache_key in redis_client.scan_iter(
            match=f"{prefix}:*", count=_EVICTION_BATCH_SIZE,
        ):
            batch.append(cache_key)
            if len(batch) == _EVICTION_BATCH_SIZE:
                redis_client.delete(*batch)
                completed += len(batch)
                batch.clear()
        if batch:
            redis_client.delete(*batch)
            completed += len(batch)
    except Exception as exc:
        # Keep completed batches when an adapter/iterator fails partway through.
        logger.warning(
            "[cache] pattern evict failed: error_type={}", type(exc).__name__,
        )
    return completed


def evict_cache_pattern(prefix: str) -> int:
    """手动按前缀批量失效, 返回删除键数."""
    return _evict_pattern(prefix)
