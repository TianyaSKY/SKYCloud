"""Admission and publication independently read current edit permission."""
import pytest
from sqlalchemy import update, delete
from app.exceptions import PermissionDeniedError
from app.features.file import upload_authorization, upload_quota
from app.models.workspace import WorkspaceMember
from app.models.upload_reservation import UploadDailyUsage


@pytest.mark.parametrize('phase', ['admit', 'publish-demote', 'publish-remove'])
def test_upload_boundary_rejects_current_permission_loss(session, test_workspace, test_user, phase):
    wid, uid = test_workspace.id, test_user.id
    # Keep a stale admin ORM identity to ensure locked reads refresh it.
    member = session.query(WorkspaceMember).filter_by(workspace_id=wid, user_id=uid).one()
    if phase != 'admit':
        reservation_id = upload_quota.reserve(session, wid, uid, 3).id
    statement = delete(WorkspaceMember) if phase == 'publish-remove' else update(WorkspaceMember).values(role='viewer')
    session.execute(statement.where(WorkspaceMember.workspace_id == wid, WorkspaceMember.user_id == uid),
                    execution_options={'synchronize_session': False})
    session.commit()
    with pytest.raises(PermissionDeniedError):
        if phase == 'admit':
            upload_authorization.reserve(session, wid, uid, 3)
        else:
            upload_authorization.consume(session, wid, uid, reservation_id, 3)
    session.rollback()
    if phase == 'admit':
        assert session.query(UploadDailyUsage).count() == 0
    else:
        assert upload_quota.find_reservation(session, reservation_id).status == 'active'
        assert session.query(UploadDailyUsage).one().attempts == 1
