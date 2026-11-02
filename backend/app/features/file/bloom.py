"""基于 Redis 的文件访问布隆过滤器, 在访问 SQL 前执行快速拦截.

假阳性是安全的: 会直接穿透回退至数据库权威鉴权。重建流程在
分布式锁保护下写入临时键, 随后原子性重命名两个键以完成替换。
"""

import hashlib
import os
import time
import uuid
from collections.abc import Iterable

from loguru import logger
from sqlalchemy import select

from app.infra.extensions import SessionLocal, redis_client
from app.models.file import File

BLOOM_BITS_PER_ITEM = max(8, int(os.getenv("FILE_ACCESS_BLOOM_BITS_PER_ITEM", "12")))
BLOOM_HASH_COUNT = max(2, int(os.getenv("FILE_ACCESS_BLOOM_HASH_COUNT", "7")))
BLOOM_MIN_BITS = max(1024, int(os.getenv("FILE_ACCESS_BLOOM_MIN_BITS", "4096")))
BLOOM_BUILD_LOCK_SECONDS = max(
    5, int(os.getenv("FILE_ACCESS_BLOOM_BUILD_LOCK_SECONDS", "30")),
)


def _scope_name(user_id: int | None) -> str:
    return "all" if user_id is None else f"user:{user_id}"


def _bits_key(user_id: int | None) -> str:
    return f"file_access:bloom:{_scope_name(user_id)}:bits"


def _meta_key(user_id: int | None) -> str:
    return f"file_access:bloom:{_scope_name(user_id)}:meta"


def _lock_key(user_id: int | None) -> str:
    return f"file_access:bloom:{_scope_name(user_id)}:lock"


def _bitmap_size(expected_items: int) -> int:
    return max(BLOOM_MIN_BITS, expected_items * BLOOM_BITS_PER_ITEM)


def _hash_positions(file_id: int, bitmap_size: int, hash_count: int) -> list[int]:
    payload = str(file_id).encode("utf-8")
    digest = hashlib.sha256(payload).digest()
    h1 = int.from_bytes(digest[:8], "big")
    h2 = int.from_bytes(digest[8:16], "big") or 1
    return [int((h1 + (idx * h2)) % bitmap_size) for idx in range(hash_count)]


def _iter_file_ids(user_id: int | None) -> Iterable[int]:
    session = SessionLocal()
    try:
        query = select(File.id)
        if user_id is not None:
            query = query.where(File.uploader_id == user_id)
        return list(session.scalars(query))
    finally:
        session.close()


def _build_filter(user_id: int | None) -> bool:
    """重建指定作用域的过滤器; 抢锁失败返回 False 且调用方回退至 SQL."""
    scope = _scope_name(user_id)
    token = uuid.uuid4().hex
    lock_key = _lock_key(user_id)

    try:
        if not redis_client.set(
            lock_key, token, nx=True, ex=BLOOM_BUILD_LOCK_SECONDS,
        ):
            return False

        file_ids = list(_iter_file_ids(user_id))
        bitmap_size = _bitmap_size(len(file_ids))
        temp_bits_key = f"{_bits_key(user_id)}:tmp:{token}"
        temp_meta_key = f"{_meta_key(user_id)}:tmp:{token}"

        pipe = redis_client.pipeline()
        pipe.setbit(temp_bits_key, 0, 0)
        for file_id in file_ids:
            for position in _hash_positions(
                file_id, bitmap_size, BLOOM_HASH_COUNT,
            ):
                pipe.setbit(temp_bits_key, position, 1)
        pipe.hset(
            temp_meta_key,
            mapping={
                "ready": "1",
                "bitmap_size": str(bitmap_size),
                "hash_count": str(BLOOM_HASH_COUNT),
                "item_count": str(len(file_ids)),
                "rebuilt_at": str(int(time.time())),
            },
        )
        pipe.execute()

        # 先写入临时键再执行重命名原子替换, 确保并发读请求
        # 绝不会观测到构建中途的半成品位图数据。
        pipe = redis_client.pipeline()
        pipe.delete(_bits_key(user_id), _meta_key(user_id))
        pipe.rename(temp_bits_key, _bits_key(user_id))
        pipe.rename(temp_meta_key, _meta_key(user_id))
        pipe.execute()
    except Exception as exc:
        logger.warning("Failed to build file access bloom for {}: {}", scope, exc)
        return False
    finally:
        try:
            if redis_client.get(lock_key) == token:
                redis_client.delete(lock_key)
        except Exception:
            logger.debug("Failed to release bloom build lock for {}", scope)
    return True


def _ensure_filter_ready(user_id: int | None) -> bool:
    try:
        ready = redis_client.hget(_meta_key(user_id), "ready")
        if ready == "1":
            return True
        return _build_filter(user_id)
    except Exception as exc:
        logger.warning(
            "Failed to prepare bloom filter for {}: {}", _scope_name(user_id), exc,
        )
        return False


def _maybe_contains(user_id: int | None, file_id: int) -> bool:
    # 过滤器不可用时保守返回 True, 由数据库执行权威鉴权。
    if not _ensure_filter_ready(user_id):
        return True

    try:
        meta = redis_client.hgetall(_meta_key(user_id))
        bitmap_size = int(meta.get("bitmap_size", "0"))
        hash_count = int(meta.get("hash_count", "0"))
        if bitmap_size <= 0 or hash_count <= 0:
            return True

        positions = _hash_positions(file_id, bitmap_size, hash_count)
        key = _bits_key(user_id)
        pipe = redis_client.pipeline(transaction=False)
        for position in positions:
            pipe.getbit(key, position)
        bits = pipe.execute()
        return all(bit == 1 for bit in bits)
    except Exception as exc:
        logger.warning(
            "Failed bloom membership check for {}: {}", _scope_name(user_id), exc,
        )
        return True


def add_file(file_id: int, user_id: int | None) -> None:
    """将单个文件 ID 添加至用户作用域, 若全局作用域存在亦一并写入."""
    scopes: list[int | None] = [None]
    if user_id is not None:
        scopes.append(user_id)

    for scope_user_id in scopes:
        try:
            meta = redis_client.hgetall(_meta_key(scope_user_id))
            if meta.get("ready") != "1":
                continue

            bitmap_size = int(meta.get("bitmap_size", "0"))
            hash_count = int(meta.get("hash_count", "0"))
            if bitmap_size <= 0 or hash_count <= 0:
                continue

            pipe = redis_client.pipeline()
            for position in _hash_positions(file_id, bitmap_size, hash_count):
                pipe.setbit(_bits_key(scope_user_id), position, 1)
            pipe.execute()
        except Exception as exc:
            logger.warning(
                "Failed to add file {} into bloom scope {}: {}",
                file_id,
                _scope_name(scope_user_id),
                exc,
            )


def maybe_user_can_access_file(user_id: int, file_id: int) -> bool:
    """返回用户是否可能拥有该文件; 返回 False 时可安全直接拒绝."""
    return _maybe_contains(user_id, file_id)


def maybe_file_exists(file_id: int) -> bool:
    """返回文件是否可能存在; 返回 False 时可安全直接返回 404."""
    return _maybe_contains(None, file_id)
