"""Spring 风格 Redis 缓存装饰器，供 service 层复用。

三种装饰器对应 Spring Boot 注解语义：

    @cacheable  — 先读缓存；未命中则执行函数并写入（Spring @Cacheable）
    @cache_evict — 执行函数后清除匹配键（Spring @CacheEvict）
    @cache_put  — 始终执行函数并回写缓存（Spring @CachePut）

读/写失败只记日志，不阻断业务；None 默认不缓存，避免穿透。
"""

import asyncio
import functools
import json
import uuid
from typing import Any, Callable, Optional

from app.infra.extensions import redis_client
from app.infra.owned_work import run_owned

from loguru import logger


# ---------------------------------------------------------------------------
# 缓存键构造
# ---------------------------------------------------------------------------

def _build_cache_key(
        prefix: str,
        args: tuple,
        kwargs: dict,
        key: Optional[Callable] = None,
) -> str:
    """拼 Redis 键：``prefix`` + 自定义 key 回调或全部位置参数。

    示例（prefix="user:profile"）：
        get_user(2)            → "user:profile:2"
        key=lambda id, **_: id → "user:profile:2"
    """
    if key is not None:
        suffix = str(key(*args, **kwargs))
        return f"{prefix}:{suffix}"

    parts = [str(a) for a in args]
    if kwargs:
        parts.extend(f"{k}={v}" for k, v in sorted(kwargs.items()))
    return f"{prefix}:{':'.join(parts)}" if parts else prefix


# ---------------------------------------------------------------------------
# @cacheable — 读穿缓存
# ---------------------------------------------------------------------------

def invalidate_cache_generation(scope: str) -> None:
    """Rotate a cache namespace before removing old physical keys."""
    try:
        redis_client.set(scope, uuid.uuid4().hex)
    except Exception as exc:
        logger.warning("[cache] generation invalidation failed: error_type={}", type(exc).__name__)


def _generation_cache_key(cache_key: str, scope: str) -> str:
    generation = redis_client.get(scope)
    if generation is None:
        candidate = uuid.uuid4().hex
        if redis_client.set(scope, candidate, nx=True):
            generation = candidate
        else:
            generation = redis_client.get(scope)
    if isinstance(generation, bytes):
        generation = generation.decode('ascii')
    if not isinstance(generation, str) or len(generation) != 32 or any(char not in '0123456789abcdef' for char in generation):
        raise ValueError('Invalid cache generation')
    return f"{cache_key}:generation:{generation}"


def cacheable(
        prefix: str,
        expire: int = 3600,
        cache_none: bool = False,
        key: Optional[Callable] = None,
        generation_key: Optional[Callable] = None,
        cache_allowed: Optional[Callable] = None,
):
    """先查 Redis，未命中再执行函数并写入。

    :param prefix: 键前缀（对应 Spring cacheNames）
    :param expire: TTL 秒，默认 3600
    :param cache_none: False 时不缓存 None（对应 unless="#result == null"）
    :param key: 可选 ``(*args, **kwargs) -> Any`` 生成键后缀
    :param cache_allowed: 可选同步检查，在读取和回写前决定是否允许共享缓存
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            if cache_allowed is not None and not cache_allowed(*args, **kwargs):
                return func(*args, **kwargs)
            cache_key = _build_cache_key(prefix, args, kwargs, key)
            try:
                if generation_key is not None:
                    cache_key = _generation_cache_key(cache_key, generation_key(*args, **kwargs))
                cached = redis_client.get(cache_key)
                if cached is not None:
                    return json.loads(cached)
            except Exception as e:
                logger.warning("[cache] read failed: error_type={}", type(e).__name__)
                if generation_key is not None:
                    cache_key = None  # Failed generation reads bypass caching.

            result = func(*args, **kwargs)
            if cache_allowed is not None and not cache_allowed(*args, **kwargs):
                return result

            if cache_key is not None and (result is not None or cache_none):
                try:
                    redis_client.setex(
                        cache_key, expire,
                        json.dumps(result, ensure_ascii=False, default=str),
                    )
                except Exception as e:
                    logger.warning("[cache] write failed: error_type={}", type(e).__name__)
            return result

        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            if cache_allowed is not None and not await run_owned(cache_allowed, *args, **kwargs):
                return await func(*args, **kwargs)
            cache_key = _build_cache_key(prefix, args, kwargs, key)
            try:
                if generation_key is not None:
                    cache_key = await run_owned(_generation_cache_key, cache_key, generation_key(*args, **kwargs))
                cached = await run_owned(redis_client.get, cache_key)
                if cached is not None:
                    return json.loads(cached)
            except Exception as e:
                logger.warning("[cache] read failed: error_type={}", type(e).__name__)
                if generation_key is not None:
                    cache_key = None  # Failed generation reads bypass caching.

            result = await func(*args, **kwargs)
            if cache_allowed is not None and not await run_owned(cache_allowed, *args, **kwargs):
                return result

            if cache_key is not None and (result is not None or cache_none):
                try:
                    await run_owned(
                        redis_client.setex,
                        cache_key, expire,
                        json.dumps(result, ensure_ascii=False, default=str),
                    )
                except Exception as e:
                    logger.warning("[cache] write failed: error_type={}", type(e).__name__)
            return result

        chosen = async_wrapper if asyncio.iscoroutinefunction(func) else sync_wrapper
        chosen.cache_prefix = prefix  # 供外部内省前缀
        return chosen

    return decorator


# ---------------------------------------------------------------------------
# @cache_evict — 写后失效
# ---------------------------------------------------------------------------

def cache_evict(
        prefix: str,
        key: Optional[Callable] = None,
        all_entries: bool = False,
        before_invocation: bool = False,
):
    """在被装饰函数执行前/后清除缓存。

    :param prefix: 键前缀
    :param key: 推导待删单键的回调
    :param all_entries: True 时按 ``prefix:*`` 批量删（Spring allEntries）
    :param before_invocation: True 时在函数执行前删除（Spring beforeInvocation）
    """

    def decorator(func: Callable) -> Callable:
        def _do_evict(args, kwargs):
            try:
                if all_entries:
                    _evict_pattern(prefix)
                else:
                    cache_key = _build_cache_key(prefix, args, kwargs, key)
                    redis_client.delete(cache_key)
            except Exception as e:
                logger.warning("[cache] evict failed: error_type={}", type(e).__name__)

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            if before_invocation:
                _do_evict(args, kwargs)
            result = func(*args, **kwargs)
            if not before_invocation:
                _do_evict(args, kwargs)
            return result

        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            if before_invocation:
                await run_owned(_do_evict, args, kwargs)
            result = await func(*args, **kwargs)
            if not before_invocation:
                await run_owned(_do_evict, args, kwargs, cancel_queued=False)
            return result

        return async_wrapper if asyncio.iscoroutinefunction(func) else sync_wrapper

    return decorator


# ---------------------------------------------------------------------------
# @cache_put — 强制回写
# ---------------------------------------------------------------------------

def cache_put(
        prefix: str,
        expire: int = 3600,
        cache_none: bool = False,
        key: Optional[Callable] = None,
):
    """始终执行函数并用结果覆盖缓存（从不先读缓存）。"""

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            result = func(*args, **kwargs)
            _put_result(prefix, args, kwargs, key, result, expire, cache_none)
            return result

        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            result = await func(*args, **kwargs)
            await run_owned(_put_result, prefix, args, kwargs, key, result, expire, cache_none, cancel_queued=False)
            return result

        return async_wrapper if asyncio.iscoroutinefunction(func) else sync_wrapper

    return decorator


def _put_result(prefix, args, kwargs, key, result, expire, cache_none):
    """写入单条缓存；None 且 cache_none=False 时跳过。"""
    if result is None and not cache_none:
        return
    cache_key = _build_cache_key(prefix, args, kwargs, key)
    try:
        redis_client.setex(
            cache_key, expire,
            json.dumps(result, ensure_ascii=False, default=str),
        )
    except Exception as e:
        logger.warning("[cache] put failed: error_type={}", type(e).__name__)


# ---------------------------------------------------------------------------
# 手动清除
# ---------------------------------------------------------------------------

def evict_cache(prefix: str, *key_parts: Any) -> None:
    """手动删除单键：``evict_cache("user:profile", user_id)``。"""
    cache_key = (
        f"{prefix}:{':'.join(str(p) for p in key_parts)}"
        if key_parts else prefix
    )
    try:
        redis_client.delete(cache_key)
    except Exception as e:
        logger.warning("[cache] manual evict failed: error_type={}", type(e).__name__)


def _evict_pattern(prefix: str) -> int:
    """分批删除匹配键，返回成功提交删除请求的键数。"""
    completed = 0
    batch = []
    try:
        for cache_key in redis_client.scan_iter(match=f"{prefix}:*", count=128):
            batch.append(cache_key)
            if len(batch) == 128:
                redis_client.delete(*batch)
                completed += len(batch)
                batch.clear()
        if batch:
            redis_client.delete(*batch)
            completed += len(batch)
    except Exception as e:
        logger.warning("[cache] pattern evict failed: error_type={}", type(e).__name__)
    return completed


def evict_cache_pattern(prefix: str) -> int:
    """手动按前缀批量失效，返回删除键数。"""
    return _evict_pattern(prefix)
