"""
Spring-style Redis cache decorators for FastAPI service layer.

Provides three decorators mirroring Spring Boot's caching annotations:

    @cacheable  — Read cache first; on miss, execute function and store result.
                  Equivalent to Spring's @Cacheable.

    @cache_evict — Execute function, then clear matching cache entries.
                   Equivalent to Spring's @CacheEvict.

    @cache_put  — Always execute function, then update cache with result.
                  Equivalent to Spring's @CachePut.

Usage example:

    from app.cache import cacheable, cache_evict, evict_cache

    @cacheable(prefix="user:profile", expire=3600)
    def get_user_dict(user_id: int) -> dict | None:
        user = db.session.get(User, user_id)
        return user.to_dict() if user else None   # None won't be cached

    @cache_evict(prefix="user:profile", key=lambda id, data: id)
    def update_user(id, data):
        ...

    # Manual eviction is also supported:
    evict_cache("user:profile", user_id)
"""

import asyncio
import functools
import json
import logging
from typing import Any, Callable, Optional

from app.extensions import redis_client

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 缓存键构造
# ---------------------------------------------------------------------------

def _build_cache_key(
    prefix: str,
    args: tuple,
    kwargs: dict,
    key: Optional[Callable] = None,
) -> str:
    """
    Build a Redis cache key.

    If *key* is provided, it is called with the same positional/keyword
    arguments as the decorated function and must return a value whose
    ``str()`` representation is used as the key suffix.

    Otherwise all positional args are joined with ``:``.

    Examples (prefix="user:profile"):
        get_user(2)            → "user:profile:2"
        get_user(2, active=1)  → "user:profile:2:active=1"
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
# @cacheable — Spring @Cacheable
# ---------------------------------------------------------------------------

def cacheable(
    prefix: str,
    expire: int = 3600,
    cache_none: bool = False,
    key: Optional[Callable] = None,
):
    """
    Read from Redis cache first; on cache miss execute the function and
    store the result.

    Args:
        prefix:     Cache key prefix (equivalent to Spring's ``cacheNames``).
        expire:     TTL in seconds (default 3600).
        cache_none: If False (default), ``None`` results are **not** cached,
                    equivalent to Spring's ``unless = "#result == null"``.
        key:        Optional callable ``(*args, **kwargs) -> Any`` that
                    produces the cache-key suffix from function arguments.
                    When omitted, all positional args are used.
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            cache_key = _build_cache_key(prefix, args, kwargs, key)
            try:
                cached = redis_client.get(cache_key)
                if cached is not None:
                    return json.loads(cached)
            except Exception as e:
                logger.warning(f"[cache] read error {cache_key}: {e}")

            result = func(*args, **kwargs)

            if result is not None or cache_none:
                try:
                    redis_client.setex(
                        cache_key, expire,
                        json.dumps(result, ensure_ascii=False, default=str),
                    )
                except Exception as e:
                    logger.warning(f"[cache] write error {cache_key}: {e}")
            return result

        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            cache_key = _build_cache_key(prefix, args, kwargs, key)
            try:
                cached = redis_client.get(cache_key)
                if cached is not None:
                    return json.loads(cached)
            except Exception as e:
                logger.warning(f"[cache] read error {cache_key}: {e}")

            result = await func(*args, **kwargs)

            if result is not None or cache_none:
                try:
                    redis_client.setex(
                        cache_key, expire,
                        json.dumps(result, ensure_ascii=False, default=str),
                    )
                except Exception as e:
                    logger.warning(f"[cache] write error {cache_key}: {e}")
            return result

        chosen = async_wrapper if asyncio.iscoroutinefunction(func) else sync_wrapper
        chosen.cache_prefix = prefix          # expose for introspection
        return chosen

    return decorator


# ---------------------------------------------------------------------------
# @cache_evict — Spring @CacheEvict
# ---------------------------------------------------------------------------

def cache_evict(
    prefix: str,
    key: Optional[Callable] = None,
    all_entries: bool = False,
    before_invocation: bool = False,
):
    """
    Clear cache entries when the decorated function is executed.

    Args:
        prefix:            Cache key prefix.
        key:               Callable to derive the specific key to evict.
        all_entries:       If True, evict **all** keys matching ``prefix:*``
                           (equivalent to Spring's ``allEntries = true``).
        before_invocation: If True, evict **before** the function runs
                           (equivalent to Spring's ``beforeInvocation``).
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
                logger.warning(f"[cache] evict error {prefix}: {e}")

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
                _do_evict(args, kwargs)
            result = await func(*args, **kwargs)
            if not before_invocation:
                _do_evict(args, kwargs)
            return result

        return async_wrapper if asyncio.iscoroutinefunction(func) else sync_wrapper

    return decorator


# ---------------------------------------------------------------------------
# @cache_put — Spring @CachePut
# ---------------------------------------------------------------------------

def cache_put(
    prefix: str,
    expire: int = 3600,
    cache_none: bool = False,
    key: Optional[Callable] = None,
):
    """
    Always execute the function and update the cache with the result.

    Unlike ``@cacheable``, this **never reads** from cache — it always
    invokes the underlying function and then writes the result back.

    Args:
        prefix:     Cache key prefix.
        expire:     TTL in seconds.
        cache_none: Whether to cache ``None`` results.
        key:        Optional callable for custom cache-key generation.
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            result = func(*args, **kwargs)
            _put_result(prefix, args, kwargs, key, result, expire, cache_none)
            return result

        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            result = await func(*args, **kwargs)
            _put_result(prefix, args, kwargs, key, result, expire, cache_none)
            return result

        return async_wrapper if asyncio.iscoroutinefunction(func) else sync_wrapper

    return decorator


def _put_result(prefix, args, kwargs, key, result, expire, cache_none):
    if result is None and not cache_none:
        return
    cache_key = _build_cache_key(prefix, args, kwargs, key)
    try:
        redis_client.setex(
            cache_key, expire,
            json.dumps(result, ensure_ascii=False, default=str),
        )
    except Exception as e:
        logger.warning(f"[cache] put error {cache_key}: {e}")


# ---------------------------------------------------------------------------
# 手动清除缓存
# ---------------------------------------------------------------------------

def evict_cache(prefix: str, *key_parts: Any) -> None:
    """
    Manually evict a single cache entry.

    Usage::

        evict_cache("user:profile", user_id)
        # deletes key "user:profile:123"
    """
    cache_key = (
        f"{prefix}:{':'.join(str(p) for p in key_parts)}"
        if key_parts else prefix
    )
    try:
        redis_client.delete(cache_key)
    except Exception as e:
        logger.warning(f"[cache] manual evict error {cache_key}: {e}")


def _evict_pattern(prefix: str) -> int:
    """Delete all keys matching ``prefix:*``."""
    try:
        pattern = f"{prefix}:*"
        keys = list(redis_client.scan_iter(match=pattern))
        if keys:
            redis_client.delete(*keys)
        return len(keys)
    except Exception as e:
        logger.warning(f"[cache] pattern evict error {prefix}: {e}")
        return 0


def evict_cache_pattern(prefix: str) -> int:
    """
    Manually evict all cache entries matching ``prefix:*``.

    Returns the number of keys deleted.
    """
    return _evict_pattern(prefix)
