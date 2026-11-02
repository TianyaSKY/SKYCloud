"""Redis 读穿缓存, 供 service 层复用.

只保留一种缓存写入方式: ``@cacheable`` 先读缓存, 未命中再执行函数并写入
(Spring ``@Cacheable`` 语义)。写路径的失效统一由 ``sql_cache_revision`` /
``sql_cache_allowed`` 提供的 SQL revision 键后缀和
``invalidate_cache_generation`` 的命名空间旋转负责; 删除无后缀键无法失效
现役条目, 因此不再提供手动单键删除或强制回写路径。

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
from app.infra.owned_work import run_owned
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


# 缓存键构造
# 拼装 Redis 键: prefix + 自定义 key 回调或位置/关键字参数
# 供 @cacheable 等缓存装饰器统一调用

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
        # ParamSpec 无法表达从已存储参数中重新构建的函数签名。
        suffix = str(cast("Callable[..., object]", key)(*args, **kwargs))
        return f"{prefix}:{suffix}"

    parts = [str(value) for value in args]
    if kwargs:
        parts.extend(f"{name}={value}" for name, value in sorted(kwargs.items()))
    return f"{prefix}:{':'.join(parts)}" if parts else prefix


# @cacheable — 读穿缓存装饰器实现
# 基于 Redis 与 SQL 版本号的双重检查读穿缓存
# 版本匹配时直接返回缓存,失效时回源查询并更新

def sql_cache_revision(session: Session, scope: str) -> int:
    """读取缓存作用域的 SQL 版本号, 缺失的作用域默认为 0."""
    return (
        session.scalar(
            select(SqlCacheRevision.revision).where(SqlCacheRevision.scope == scope),
        )
        or 0
    )


def sql_cache_allowed(session: Session, *args: object, **kwargs: object) -> bool:
    """将共享 SQL 缓存排除在可能回滚写入的事务之外."""
    del args, kwargs
    if session.new or session.dirty or session.deleted:
        return False
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        # Flush/原生 SQL 可能清空 ORM 脏状态, 但写入事务依然处于活跃中。
        return session.scalar(text("SELECT pg_current_xact_id_if_assigned()")) is None
    if dialect == "sqlite":
        driver = cast(
            "_SQLiteDriver", session.connection().connection.driver_connection,
        )
        return not driver.in_transaction
    return not session.in_transaction()


def invalidate_cache_generation(scope: str) -> None:
    """在清除旧的物理键之前轮转缓存命名空间."""
    try:
        redis_client.set(scope, uuid.uuid4().hex)
    except Exception as exc:
        # Redis 适配层可能发生任意异常; 失效失败绝不得撤销业务写入。
        logger.warning(
            "[cache] generation invalidation failed: error_type={}",
            type(exc).__name__,
        )


class CachedFunction(Protocol[_P, _R_co]):
    """装饰后的可调用对象; 经 ``cache_prefix`` 暴露缓存前缀供外部内省."""

    cache_prefix: str

    def __call__(self, *args: _P.args, **kwargs: _P.kwargs) -> _R_co:
        """以原始签名调用被装饰的业务函数."""
        ...


class _CacheableDecorator(Protocol):
    """不约束函数签名的缓存装饰器协议."""

    def __call__(self, func: Callable[_P, _R]) -> CachedFunction[_P, _R]: ...


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
    """配置读穿缓存而不改变被包装函数的签名."""

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
            # 代际回调、Redis 和 JSON 可能失败; 保留 SQL 回退。
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
            # 保留回退机制, 且不捕获 BaseException/任务取消。
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
            # 序列化和 Redis 失败绝不得替代业务处理结果。
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
            # 将序列化保留在事件循环中, 并在取消时排空所属 Redis I/O。
            logger.warning("[cache] write failed: error_type={}", type(exc).__name__)

    def __call__(self, func: Callable[_P, _R]) -> CachedFunction[_P, _R]:
        chosen = (
            self._wrap_async(func)
            if inspect.iscoroutinefunction(func)
            else self._wrap_sync(func)
        )
        # 实际包装器是一个函数, 支持为其设置自定义属性。
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
    """读穿缓存: 优先查 Redis, 未命中则执行函数并写入缓存.

    仅本装饰器写入缓存条目。失效机制依赖键回调函数:
    ``key``/``generation_key`` 将条目绑定到持久化 SQL
    版本号或轮转命名空间, 从而确保已提交写入立即可见,
    而无需删除无后缀的物理键。

    ``CacheableParams`` 保存前缀、TTL 与 ``None`` 策略。
    ``key``、``generation_key`` 以及 ``cache_allowed``
    接收被装饰函数的参数。
    """
    return _Cacheable(
        params, key=key, generation_key=generation_key, cache_allowed=cache_allowed,
    )


# 按前缀批量失效
# 扫描并清理指定前缀的 Redis 缓存键
# 供数据变更时批量清理关联缓存

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
        # 当适配层/迭代器中途失败时, 保留已完成处理的批次。
        logger.warning(
            "[cache] pattern evict failed: error_type={}", type(exc).__name__,
        )
    return completed


def evict_cache_pattern(prefix: str) -> int:
    """手动按前缀批量失效, 返回删除键数."""
    return _evict_pattern(prefix)
