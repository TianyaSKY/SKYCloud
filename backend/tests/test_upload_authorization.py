"""准入与发布独立读取当前编辑权限测试."""

import pytest
from sqlalchemy import delete, update
from sqlalchemy.orm import Session

from app.exceptions import PermissionDeniedError
from app.features.file import upload_authorization, upload_quota
from app.models.upload_reservation import UploadDailyUsage
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember


@pytest.mark.parametrize(
    "phase", ["admit", "publish-demote", "publish-remove"]
)
def test_upload_boundary_rejects_current_permission_loss(
    session: Session, test_workspace: Workspace, test_user: User, phase: str
) -> None:
    wid, uid = int(test_workspace.id), int(test_user.id)
    # 保留过期的管理员 ORM 标识以确保加锁读取能刷新它.
    _member = (
        session.query(WorkspaceMember)
        .filter_by(workspace_id=wid, user_id=uid)
        .one()
    )
    reservation_id = ""
    if phase != "admit":
        reservation_id = upload_quota.reserve(
            session,
            upload_quota.ReserveParams(workspace_id=wid, user_id=uid, size=3),
        ).id
    statement = (
        delete(WorkspaceMember)
        if phase == "publish-remove"
        else update(WorkspaceMember).values(role="viewer")
    )
    session.execute(
        statement.where(
            WorkspaceMember.workspace_id == wid, WorkspaceMember.user_id == uid
        ),
        execution_options={"synchronize_session": False},
    )
    session.commit()
    with pytest.raises(PermissionDeniedError):
        if phase == "admit":
            upload_authorization.reserve(
                session,
                upload_quota.ReserveParams(
                    workspace_id=wid, user_id=uid, size=3
                ),
            )
        else:
            upload_authorization.consume(session, wid, uid, reservation_id, 3)
    session.rollback()
    if phase == "admit":
        assert session.query(UploadDailyUsage).count() == 0
    else:
        res = upload_quota.find_reservation(session, reservation_id)
        assert res is not None and res.status == "active"
        assert session.query(UploadDailyUsage).one().attempts == 1
