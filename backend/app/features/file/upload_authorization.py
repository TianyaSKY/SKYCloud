"""上传准入与 SQL 发布边界上的实时编辑权限校验."""

from sqlalchemy.orm import Session

from app.features.file import upload_quota
from app.features.file.upload_quota import ReserveParams
from app.features.workspace.permissions import assert_can_write_locked
from app.models.upload_reservation import UploadReservation


def reserve(session: Session, params: ReserveParams) -> UploadReservation:
    """校验当前工作空间写权限, 随后执行预留记录准入."""
    assert_can_write_locked(session, params.workspace_id, params.user_id)
    return upload_quota.reserve(session, params)


def consume(
    session: Session,
    workspace_id: int,
    user_id: int,
    reservation_id: str,
    size: int,
) -> UploadReservation:
    """校验当前工作空间写权限, 随后将预留记录标记为已完成."""
    # 调用方在本事务中发布文件及任务收件箱, 中间无额外提交。
    # 任何角色权限变更都必须等待该事务决议后生效。
    assert_can_write_locked(session, workspace_id, user_id)
    return upload_quota.consume(session, workspace_id, user_id, reservation_id, size)
