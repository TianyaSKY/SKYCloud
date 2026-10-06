"""File deletion must never discard physical bytes before metadata commit."""
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session

from app.features.file import service
from app.infra.file_object_deletion import delete_pending_objects
from app.models.file import File
from app.models.file_object_deletion import FileObjectDeletion
from app.infra.datetime_utils import utc_now


def owned_factory(session):
    return lambda: Session(bind=session.get_bind(),join_transaction_mode="create_savepoint")


def test_failed_metadata_commit_keeps_object_and_rolls_back_intent(session,test_workspace,test_user,monkeypatch):
    file = File(name="keep",file_path="keep",workspace_id=test_workspace.id,uploader_id=test_user.id)
    session.add(file)
    session.flush()
    file_id = file.id
    storage = MagicMock()
    monkeypatch.setattr(service,"get_storage_client",lambda:storage)
    with owned_factory(session)() as owned:
        monkeypatch.setattr(owned,"commit",MagicMock(side_effect=RuntimeError("commit failed")))
        with pytest.raises(RuntimeError,match="commit failed"):
            service.delete_file(owned,file_id)
        owned.rollback()
    storage.delete_file.assert_not_called()
    assert session.get(File,file_id) is not None
    assert session.query(FileObjectDeletion).count() == 0


def test_deletion_intent_retries_and_ignores_deleted_metadata_fk(session,test_workspace,test_user,monkeypatch):
    file = File(name="remove",file_path="remove",workspace_id=test_workspace.id,uploader_id=test_user.id)
    session.add(file)
    session.flush()
    file_id = file.id
    monkeypatch.setattr(service.change_log_service,"log_event",lambda **kwargs:None)
    service.delete_file(session,file_id)
    assert session.get(File,file_id) is None
    intent = session.query(FileObjectDeletion).one()
    storage = MagicMock()
    storage.delete_file.side_effect = RuntimeError("storage unavailable")
    assert delete_pending_objects(session_factory=owned_factory(session),storage_factory=lambda:storage) == 0
    session.expire_all()
    assert intent.completed_at is None and intent.attempts == 1
    assert intent.last_error_type == "RuntimeError"
    intent.next_attempt_at = utc_now()
    session.flush()
    storage.delete_file.side_effect = None
    assert delete_pending_objects(session_factory=owned_factory(session),storage_factory=lambda:storage) == 1
    session.expire_all()
    assert intent.completed_at is not None and intent.attempts == 2
    assert delete_pending_objects(session_factory=owned_factory(session),storage_factory=lambda:storage) == 0


def test_shared_reference_retains_object_then_last_delete_creates_new_intent(session,test_workspace,test_user,monkeypatch):
    files = [File(name=name,file_path="shared",workspace_id=test_workspace.id,uploader_id=test_user.id)
             for name in ("one","two")]
    session.add_all(files)
    session.flush()
    ids = [file.id for file in files]
    monkeypatch.setattr(service.change_log_service,"log_event",lambda **kwargs:None)
    storage = MagicMock()
    service.delete_file(session,ids[0])
    assert delete_pending_objects(session_factory=owned_factory(session),storage_factory=lambda:storage) == 1
    storage.delete_file.assert_not_called()
    service.delete_file(session,ids[1])
    assert delete_pending_objects(session_factory=owned_factory(session),storage_factory=lambda:storage) == 1
    storage.delete_file.assert_called_once_with("shared")


def test_recursive_delete_rollback_retains_records_and_staged_intents(session,test_workspace,test_user,test_folder,monkeypatch):
    from app.features.folder import service as folders
    file = File(name="child",file_path="child",workspace_id=test_workspace.id,
                uploader_id=test_user.id,parent_id=test_folder.id)
    session.add(file)
    session.flush()
    file_id,folder_id = file.id,test_folder.id
    with owned_factory(session)() as owned:
        monkeypatch.setattr(owned,"commit",MagicMock(side_effect=RuntimeError("commit failed")))
        with pytest.raises(RuntimeError,match="commit failed"):
            folders.delete_folder(owned,folder_id)
    assert session.get(File,file_id) is not None
    assert session.query(FileObjectDeletion).count() == 0


@pytest.mark.parametrize('kind',['file','folder','cycle'])
def test_recursive_delete_rejects_cross_workspace_edges_and_cycles(session,test_workspace,test_user,test_folder,kind):
    from app.features.folder import service as folders
    from app.models.workspace import Workspace
    from app.models.folder import Folder
    from app.exceptions import DomainError
    if kind == 'cycle':
        child = Folder(name='cycle',workspace_id=test_workspace.id,parent_id=test_folder.id)
        session.add(child)
        session.flush()
        test_folder.parent_id = child.id
    else:
        foreign = Workspace(name='foreign',owner_id=test_user.id)
        session.add(foreign)
        session.flush()
        if kind == 'file':
            session.add(File(name='private',file_path='private',workspace_id=foreign.id,
                             uploader_id=test_user.id,parent_id=test_folder.id))
        else:
            session.add(Folder(name='private',workspace_id=foreign.id,parent_id=test_folder.id))
    session.flush()
    folder_id = test_folder.id
    counts = session.query(File).count(),session.query(Folder).count()
    with owned_factory(session)() as owned:
        with pytest.raises(DomainError):
            folders.delete_folder(owned,folder_id)
    assert (session.query(File).count(),session.query(Folder).count()) == counts
    assert session.query(FileObjectDeletion).count() == 0
