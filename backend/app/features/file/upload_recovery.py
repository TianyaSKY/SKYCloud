"""尽力而为的上传失败补偿机制; 持久化预留表可靠保留未决清理状态."""

from collections.abc import Iterable
from dataclasses import dataclass

from loguru import logger
from sqlalchemy.orm import Session

from app.features.file import repository, upload_quota
from app.features.file.upload_quota import CancelParams
from app.features.file.upload_types import UploadStorage


@dataclass(frozen=True, slots=True, kw_only=True)
class ReleaseFailedUploadParams:
    """需要执行清理补偿的失败上传任务标识信息."""

    workspace_id: int
    uploader_id: int
    reservation_id: str | None
    object_name: str


def _release_failed_upload(
    session: Session,
    params: ReleaseFailedUploadParams,
    storage: UploadStorage | None,
) -> None:
    """释放单条上传预留记录并在该对象无其他引用时将其删除."""
    if params.reservation_id is None:
        return
    # 即使客户端未收到 ACK, 事务也可能已成功提交。在补偿
    # 远程副作用之前, 必须重新刷新持久化预留状态。
    reservation = upload_quota.lock_for_cancellation(
        session,
        params.workspace_id,
        params.uploader_id,
        params.reservation_id,
        missing_ok=True,
    )
    if reservation is None:
        return
    if reservation.status == "completed":
        session.rollback()
        return
    referenced = repository.has_object_reference(session, params.object_name)
    deleted = False
    if storage is not None and not referenced:
        try:
            # 即使 PUT 请求失败, 对象也可能已在存储端创建。删除
            # 预留的唯一对象键, 哪怕 upload_file 未报告成功。
            storage.delete_file(params.object_name)
            deleted = True
        except Exception:
            logger.warning(
                "Object deletion deferred for upload {}", params.reservation_id,
            )
    upload_quota.cancel(
        session,
        CancelParams(
            workspace_id=params.workspace_id,
            user_id=params.uploader_id,
            reservation_id=params.reservation_id,
            missing_ok=True,
            object_deleted=deleted,
        ),
    )


def _reset_failed_transaction(session: Session) -> bool:
    """丢弃失败的数据库事务, 必要时回退至底层数据库连接作废."""
    try:
        session.rollback()
    except Exception as exc:
        logger.warning("Upload rollback failed: error_type={}", type(exc).__name__)
    else:
        return True
    try:
        # SQLAlchemy 作废机制会直接丢弃坏连接; 随后同一
        # 会话可开启全新事务以执行流水对账。
        session.invalidate()
    except Exception as exc:
        logger.warning(
            "Upload connection invalidation failed: error_type={}", type(exc).__name__,
        )
        return False
    return True


def compensate_failed_uploads(
    session: Session,
    workspace_id: int,
    uploader_id: int,
    uploads: Iterable[tuple[str | None, str]],
    storage: UploadStorage | None,
) -> None:
    """保留调用方原有异常并在独立事务中继续完成预留资源清理."""
    if not _reset_failed_transaction(session):
        return
    for reservation_id, object_name in uploads:
        params = ReleaseFailedUploadParams(
            workspace_id=workspace_id,
            uploader_id=uploader_id,
            reservation_id=reservation_id,
            object_name=object_name,
        )
        try:
            _release_failed_upload(session, params, storage)
        except Exception as exc:
            logger.warning(
                "Upload compensation deferred: error_type={}", type(exc).__name__,
            )
            # 准入流水已在对象 IO 前提交。保留失败条目交由
            # 过期清理 Worker 兜底, 并继续尝试处理其余条目。
            if not _reset_failed_transaction(session):
                break
