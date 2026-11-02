"""基于现有用户与工作空间行串行化的持久化上传配额准入控制.

预留记录在对象 IO 前提交; 消费预留与插入文件记录共享同一事务。
每日准入扣减在客户端中止与 IO 故障后依然生效。
所有截止时间与日期边界均采用 UTC, 并以 UTC 时间戳存储。
"""

import re
import uuid
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TypeAlias, cast

from loguru import logger
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.orm import Session

from app.exceptions import (
    BusinessRuleError,
    ConflictError,
    PermissionDeniedError,
    ResourceNotFoundError,
)
from app.features.file.upload_types import UploadStorage
from app.infra.multipart_cleanup import remove_multipart_upload
from app.infra.upload_limits import positive_limit
from app.infra.upload_staging import staging_guard
from app.models.file import File
from app.models.upload_reservation import UploadDailyUsage, UploadReservation
from app.models.user import User
from app.models.workspace import Workspace

USER_CAPACITY = positive_limit("UPLOAD_USER_CAPACITY", 10 * 1024**3)
WORKSPACE_CAPACITY = positive_limit("UPLOAD_WORKSPACE_CAPACITY", 50 * 1024**3)
DAILY_BYTES = positive_limit("UPLOAD_DAILY_BYTES", 5 * 1024**3)
DAILY_ATTEMPTS = positive_limit("UPLOAD_DAILY_ATTEMPTS", 200)
USER_CONCURRENCY = positive_limit("UPLOAD_USER_CONCURRENCY", 8)
WORKSPACE_CONCURRENCY = positive_limit("UPLOAD_WORKSPACE_CONCURRENCY", 32)
TTL_SECONDS = positive_limit("UPLOAD_RESERVATION_TTL_SECONDS", 3600)

# 未配置暂存根目录时使用的空操作保护上下文返回值。
_GUARD_ACQUIRED = True

StorageFactory: TypeAlias = Callable[[], UploadStorage]


@dataclass(frozen=True, slots=True, kw_only=True)
class ReserveParams:
    """单次上传预留记录的准入校验输入参数."""

    workspace_id: int
    user_id: int
    size: int
    kind: str = "file"
    reservation_id: str | None = None
    details: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class CancelParams:
    """单个上传预留记录的取消入参."""

    workspace_id: int
    user_id: int
    reservation_id: str
    missing_ok: bool = False
    object_deleted: bool = False


def utc_now() -> datetime:
    """获取并返回当前 UTC 时间戳."""
    return datetime.now(UTC)


def find_reservation(
    session: Session, reservation_id: str,
) -> UploadReservation | None:
    """无锁查询上传预留记录; 调用方必须先通过 owned() 完成校验."""
    return session.get(UploadReservation, reservation_id)


def active_reservation_ids(session: Session) -> set[tuple[str, str]]:
    """返回仍处于活跃状态的 (workspace_id, reservation_id) 二元组列表."""
    rows = session.execute(
        select(UploadReservation.workspace_id, UploadReservation.id).where(
            UploadReservation.status == "active",
            UploadReservation.expires_at > utc_now(),
        ),
    )
    return {
        (str(workspace_id), reservation_id)
        for workspace_id, reservation_id in rows
    }


def lock_owners(session: Session, workspace_id: int, user_id: int) -> None:
    """按规范先后顺序对所有者用户与工作空间行同时加行锁."""
    if (
        session.scalar(select(User.id).where(User.id == user_id).with_for_update())
        is None
    ):
        message = "Upload owner not found"
        raise ResourceNotFoundError(message)
    if (
        session.scalar(
            select(Workspace.id).where(Workspace.id == workspace_id).with_for_update(),
        )
        is None
    ):
        message = "Workspace not found"
        raise ResourceNotFoundError(message)


def owned(
    session: Session,
    workspace_id: int,
    user_id: int,
    reservation_id: str,
    *,
    lock: bool = True,
) -> UploadReservation:
    """返回调用方拥有的活跃预留记录, 可选是否加行锁."""
    if lock:
        lock_owners(session, workspace_id, user_id)
    query = select(UploadReservation).where(UploadReservation.id == reservation_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    row = session.scalar(query)
    if row is None:
        message = "Upload session not found"
        raise ResourceNotFoundError(message)
    if row.workspace_id != workspace_id or row.user_id != user_id:
        message = "Upload session belongs to another user or workspace"
        raise PermissionDeniedError(message)
    if row.status != "active" or row.expires_at <= utc_now():
        message = "Upload session is closed or expired"
        raise ConflictError(message)
    return row


def reserve(session: Session, params: ReserveParams) -> UploadReservation:
    """依据存储容量、并发连接数及每日配额额度执行单次上传准入."""
    if params.size < 0:
        message = "Invalid upload size"
        raise BusinessRuleError(message)
    lock_owners(session, params.workspace_id, params.user_id)
    reservation_id = params.reservation_id or uuid.uuid4().hex
    if session.get(UploadReservation, reservation_id) is not None:
        message = "Upload session already exists"
        raise ConflictError(message)
    now = utc_now()
    active = [
        UploadReservation.status == "active",
        UploadReservation.expires_at > now,
    ]
    pending_bytes = or_(
        and_(*active),
        and_(
            UploadReservation.kind != "instant",
            UploadReservation.status != "completed",
            UploadReservation.cleaned_at.is_(None),
            UploadReservation.details["object_name"].as_string().is_not(None),
        ),
    )
    scopes = [
        (
            UploadReservation.user_id,
            params.user_id,
            USER_CAPACITY,
            USER_CONCURRENCY,
            File.uploader_id,
        ),
        (
            UploadReservation.workspace_id,
            params.workspace_id,
            WORKSPACE_CAPACITY,
            WORKSPACE_CONCURRENCY,
            File.workspace_id,
        ),
    ]
    for column, owner, capacity, concurrency, file_column in scopes:
        stored = session.scalar(
            select(func.coalesce(func.sum(File.file_size), 0)).where(
                file_column == owner,
            ),
        ) or 0
        pending = session.scalar(
            select(func.coalesce(func.sum(UploadReservation.size), 0)).where(
                column == owner, pending_bytes,
            ),
        ) or 0
        count = session.scalar(
            select(func.count())
            .select_from(UploadReservation)
            .where(column == owner, *active),
        ) or 0
        if stored + pending + params.size > capacity:
            message = "Upload storage capacity exceeded"
            raise BusinessRuleError(message)
        if count >= concurrency:
            message = "Too many active uploads"
            raise ConflictError(message)
    usage = session.get(
        UploadDailyUsage, (params.user_id, now.date()), populate_existing=True,
    )
    if usage is None:
        usage = UploadDailyUsage(
            user_id=params.user_id, day=now.date(), attempts=0, bytes=0,
        )
        session.add(usage)
    if usage.attempts >= DAILY_ATTEMPTS or usage.bytes + params.size > DAILY_BYTES:
        message = "Daily upload allowance exceeded"
        raise BusinessRuleError(message)
    usage.attempts += 1
    usage.bytes += params.size
    row = UploadReservation(
        id=reservation_id,
        user_id=params.user_id,
        workspace_id=params.workspace_id,
        size=params.size,
        kind=params.kind,
        status="active",
        created_at=now,
        expires_at=now + timedelta(seconds=TTL_SECONDS),
        details=dict(params.details) if params.details else {},
    )
    session.add(row)
    session.commit()
    return row


def consume(
    session: Session,
    workspace_id: int,
    user_id: int,
    reservation_id: str,
    size: int,
) -> UploadReservation:
    """在调用方的发布事务内将预留记录标记为已完成."""
    row = owned(session, workspace_id, user_id, reservation_id)
    if size != row.size:
        message = "Upload size differs from reservation"
        raise BusinessRuleError(message)
    row.status = "completed"
    # 调用方将其随 File 一同提交; 回滚时仍保持 active 状态。
    return row


def lock_for_cancellation(
    session: Session,
    workspace_id: int,
    user_id: int,
    reservation_id: str,
    *,
    missing_ok: bool = False,
) -> UploadReservation | None:
    """在执行破坏性清理前, 在准入行锁保护下刷新最新已提交状态."""
    lock_owners(session, workspace_id, user_id)
    row = session.scalar(
        select(UploadReservation)
        .where(UploadReservation.id == reservation_id)
        .with_for_update()
        .execution_options(populate_existing=True),
    )
    if row is None and missing_ok:
        session.rollback()
        return None
    if row is None or row.user_id != user_id or row.workspace_id != workspace_id:
        message = "Upload session belongs to another user or workspace"
        raise PermissionDeniedError(message)
    return row


def cancel(session: Session, params: CancelParams) -> None:
    """取消未完成的预留并在已删除时解除其关联的对象存储引用."""
    row = lock_for_cancellation(
        session,
        params.workspace_id,
        params.user_id,
        params.reservation_id,
        missing_ok=params.missing_ok,
    )
    if row is None:
        return
    if row.status == "active":
        row.status = "cancelled"
    if params.object_deleted and row.status != "completed":
        row.details = {**row.details, "object_name": None}
    session.commit()


def _reclaim_expired_object(
    session: Session,
    reservation_id: str,
    row: UploadReservation,
    storage_factory: StorageFactory | None,
) -> bool:
    """删除过期会话中未被引用的底层对象; 返回 False 表示需稍后重试."""
    object_name = cast("str | None", row.details.get("object_name"))
    if not object_name:
        return True
    referenced = session.scalar(
        select(File.id).where(File.file_path == object_name).limit(1),
    )
    if referenced is not None:
        return True
    if storage_factory is None:
        return False
    # 对象删除成功前不得确认清理。若底层存储发生故障,
    # 该流水条目将继续保留并等待 Worker 下一轮重试。
    try:
        storage_factory().delete_file(object_name)
    except Exception:
        session.rollback()
        logger.warning(
            "Upload object cleanup deferred for reservation {}", reservation_id,
        )
        return False
    return True


def cleanup(
    session: Session,
    multipart_root: str,
    *,
    upload_root: str | None = None,
    storage_factory: StorageFactory | None = None,
) -> int:
    """在与写入方完全一致的锁保护下回收过期或已关闭的会话."""
    now = utc_now()
    rows = session.execute(
        select(
            UploadReservation.id,
            UploadReservation.workspace_id,
            UploadReservation.user_id,
        )
        .where(
            UploadReservation.cleaned_at.is_(None),
            or_(
                UploadReservation.status != "active",
                UploadReservation.expires_at <= now,
            ),
        )
        .order_by(UploadReservation.expires_at)
        .limit(1000),
    ).all()
    removed = 0
    for reservation_id, workspace_id, user_id in rows:
        lock_owners(session, workspace_id, user_id)
        row = session.scalar(
            select(UploadReservation)
            .where(UploadReservation.id == reservation_id)
            .with_for_update()
            .execution_options(populate_existing=True),
        )
        if (
            row is None
            or row.cleaned_at is not None
            or (row.status == "active" and row.expires_at > now)
        ):
            session.rollback()
            continue
        guard = (
            staging_guard(
                upload_root, cast("str | None", row.details.get("stage_id")),
            )
            if upload_root
            else nullcontext(_GUARD_ACQUIRED)
        )
        with guard as acquired:
            if not acquired:
                session.rollback()
                continue
            if row.status == "active":
                row.status = "expired"
            if not _reclaim_expired_object(
                session, reservation_id, row, storage_factory,
            ):
                session.rollback()
                continue
            # 数据库记录的标识符绝不可生成超出上传根目录之外的路径。
            if workspace_id > 0 and re.fullmatch(
                r"[A-Za-z0-9_-]{8,128}", reservation_id,
            ):
                removed += int(
                    remove_multipart_upload(
                        multipart_root, workspace_id, reservation_id,
                    ),
                )
            row.cleaned_at = now
            session.commit()
    # 缓存近期已关闭会话的响应; 准入额度扣减生命周期长于会话。
    session.execute(
        delete(UploadReservation).where(
            UploadReservation.cleaned_at.is_not(None),
            UploadReservation.expires_at < now - timedelta(days=7),
        ),
    )
    session.execute(
        delete(UploadDailyUsage).where(
            UploadDailyUsage.day < (now - timedelta(days=90)).date(),
        ),
    )
    session.commit()
    return removed
