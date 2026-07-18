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

def cacheable(
    prefix: str,
    expire: int = 3600,
    cache_none: bool = False,
    key: Optional[Callable] = None,
):
    """先查 Redis，未命中再执行函数并写入。

    :param prefix: 键前缀（对应 Spring cacheNames）
    :param expire: TTL 秒，默认 3600
    :param cache_none: False 时不缓存 None（对应 unless="#result == null"）
    :param key: 可选 ``(*args, **kwargs) -> Any`` 生成键后缀
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
            _put_result(prefix, args, kwargs, key, result, expire, cache_none)
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
        logger.warning(f"[cache] put error {cache_key}: {e}")


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
        logger.warning(f"[cache] manual evict error {cache_key}: {e}")


def _evict_pattern(prefix: str) -> int:
    """删除 ``prefix:*`` 下全部键，返回删除数量。"""
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
    """手动按前缀批量失效，返回删除键数。"""
    return _evict_pattern(prefix)
