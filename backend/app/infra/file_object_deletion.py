"""持久化、带引用校验且与秒传准入串行化的对象清理机制."""

import hashlib
from collections.abc import Callable
from datetime import timedelta

from loguru import logger
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.features.file.upload_types import UploadStorage
from app.infra.datetime_utils import utc_now
from app.infra.extensions import SessionFactory, SessionLocal
from app.infra.storage import get_storage_client
from app.models.file import File
from app.models.file_object_deletion import FileObjectDeletion


def lock_object_path(session: Session, object_name: str) -> None:
    """在 PG 中使对象清理操作与秒传准入校验串行化.

    哈希冲突仅会使不相关的键串行化。SQLite 仅作为单写入者测试缝隙;
    生产部署与迁移必须使用 PostgreSQL。
    """
    if session.get_bind().dialect.name == "postgresql":
        digest = hashlib.sha256(object_name.encode()).digest()
        key = int.from_bytes(digest[:4], "big", signed=True)
        session.execute(
            text("SELECT pg_advisory_xact_lock(731948214, :key)"),
            {"key": key},
        )


def stage_object_deletion_fallback(session: Session, object_name: str) -> None:
    """为非 PG 测试桥接环境暂存删除意图."""
    # PostgreSQL 的 AFTER DELETE 触发器负责所有路径的暂存。
    if session.get_bind().dialect.name != "postgresql":
        session.add(FileObjectDeletion(object_name=object_name))


def delete_pending_objects(
    *,
    session_factory: SessionFactory = SessionLocal,
    storage_factory: Callable[[], UploadStorage] = get_storage_client,
    limit: int = 20,
) -> int:
    """重试幂等删除操作; 已提交的引用始终保护底层对象文件."""
    completed = 0
    for _ in range(limit):
        with session_factory() as session:
            now = utc_now()
            intent = (
                session.query(FileObjectDeletion)
                .filter(
                    FileObjectDeletion.completed_at.is_(None),
                    FileObjectDeletion.next_attempt_at <= now,
                )
                .order_by(FileObjectDeletion.id)
                .with_for_update(skip_locked=True)
                .first()
            )
            if intent is None:
                break
            lock_object_path(session, intent.object_name)
            intent.attempts += 1
            try:
                referenced = (
                    session.query(File.id)
                    .filter(File.file_path == intent.object_name)
                    .first()
                )
                if referenced is None:
                    storage_factory().delete_file(intent.object_name)
            except Exception as error:
                # 尽最大努力清理: 存储 SDK 异常集合开放, 且已提交的
                # 引用可能在并发场景下再次出现。
                intent.last_error_type = type(error).__name__[:96]
                intent.next_attempt_at = now + timedelta(
                    seconds=min(300, 2 ** min(intent.attempts, 9)),
                )
                session.commit()
                logger.warning(
                    "Object cleanup {} deferred ({})",
                    intent.id,
                    intent.last_error_type,
                )
            else:
                intent.completed_at = utc_now()
                intent.last_error_type = None
                session.commit()
                completed += 1
    return completed
