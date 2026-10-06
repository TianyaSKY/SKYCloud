"""Exercise SQL boundaries through real caller-owned transactions."""
from sqlalchemy.orm import Session

from app.features.file import repository
from app.models.file import File
from app.models.folder import Folder
from app.models.workspace import Workspace


def test_directory_pages_folder_first_and_escape_literal_names(session, test_workspace, test_user):
    root = repository.find_child_folder(session, test_workspace.id, None)
    foreign = Workspace(name="foreign", owner_id=test_user.id)
    session.add(foreign)
    session.flush()
    session.add_all([
        Folder(name="a%", workspace_id=test_workspace.id, parent_id=root.id),
        Folder(name="b", workspace_id=test_workspace.id, parent_id=root.id),
        *[File(name=name, file_path=name, workspace_id=test_workspace.id,
               uploader_id=test_user.id, parent_id=root.id) for name in ("c%", "c_", "d")],
        File(name="foreign%",file_path="foreign",workspace_id=foreign.id,uploader_id=test_user.id),
    ])
    session.flush()
    pages = [repository.list_directory(session,test_workspace.id,root.id,p,2,None,"name","asc") for p in (1,2,3)]
    assert all(page.total == 5 for page in pages)
    assert [row.name for row in pages[0].folders] == ["a%","b"]
    assert pages[0].files == [] and pages[1].folders == []
    assert [row.name for row in pages[1].files] == ["c%","c_"]
    assert [row.name for row in pages[2].files] == ["d"]
    literal = repository.list_directory(session,test_workspace.id,root.id,1,10,"%","name","asc")
    assert literal.total == 2
    assert [row.name for row in literal.folders + literal.files] == ["a%","c%"]
    fuzzy = repository.search_names(session,test_workspace.id,"%",1,10)
    assert fuzzy.total == 1 and fuzzy.items[0].name == "c%"


def test_repository_writes_do_not_commit_or_take_session_ownership(session, test_workspace, test_user):
    created = File(name="rollback.txt",file_path="rollback",workspace_id=test_workspace.id,
                   uploader_id=test_user.id,status="fail")
    with Session(bind=session.get_bind(),join_transaction_mode="create_savepoint") as owned:
        repository.insert_file(owned,created)
        file_id = created.id
        assert repository.failed_file_ids(owned,test_workspace.id) == [file_id]
        repository.mark_pending(owned,[file_id])
        owned.expire_all()
        assert repository.find_file(owned,file_id).status == "pending"
        owned.rollback()
    assert repository.find_file(session,file_id) is None
    assert session.in_transaction()


def test_object_reference_lookup_and_delete_rollback(session, test_workspace, test_user):
    files = [File(name=name,file_path="shared",workspace_id=test_workspace.id,
                  uploader_id=test_user.id) for name in ("one","two")]
    repository.insert_files(session,files)
    assert repository.has_other_object_reference(session,"shared",files[0].id)
    with Session(bind=session.get_bind(),join_transaction_mode="create_savepoint") as owned:
        second = repository.find_file(owned,files[1].id)
        repository.remove_file(owned,second)
        owned.flush()
        assert not repository.has_other_object_reference(owned,"shared",files[0].id)
        owned.rollback()
    assert repository.has_other_object_reference(session,"shared",files[0].id)
