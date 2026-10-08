"""Folder mutations preserve the canonical root and never admit cyclic moves."""
from unittest.mock import patch
import pytest
from app.exceptions import BusinessRuleError, PermissionDeniedError
from app.features.folder import service
from app.features.folder.organize import tools
from app.models.folder import Folder
from app.models.file import File


def test_service_rejects_self_descendant_and_root_mutations(session, test_workspace, test_folder):
    child=Folder(name='child',workspace_id=test_workspace.id,parent_id=test_folder.id)
    session.add(child);session.commit()
    ids=(test_folder.id,child.id,test_folder.parent_id)
    for moving,parent in [(ids[0],ids[0]),(ids[0],ids[1])]:
        with pytest.raises(BusinessRuleError):service.update_folder(session,moving,{'parent_id':parent})
        assert session.get(Folder,ids[0]).parent_id == ids[2]
    for payload in [{'name':'renamed-root'},{'parent_id':ids[0]}]:
        with pytest.raises(BusinessRuleError):service.update_folder(session,ids[2],payload)
    with pytest.raises(BusinessRuleError):service.delete_folder(session,ids[2])
    assert session.get(Folder,ids[2]).parent_id is None


def test_null_parent_creation_and_move_use_the_existing_root(session,test_workspace,test_folder):
    root=test_folder.parent_id
    created=service.create_folder(session,{'name':'new','workspace_id':test_workspace.id,'parent_id':None})
    created_id=created.id
    assert created.parent_id == root
    service.update_folder(session,created_id,{'parent_id':test_folder.id})
    assert service.update_folder(session,created_id,{'parent_id':None}).parent_id == root
    assert session.query(Folder).filter_by(workspace_id=test_workspace.id,parent_id=None).count() == 1


def test_stored_cycles_and_cross_workspace_ancestry_fail_serialization():
    row=Folder(id=1,name='loop',workspace_id=1,parent_id=1)
    row.parent=row
    with pytest.raises(BusinessRuleError):row.to_dict()
    foreign=Folder(id=2,name='private-foreign-name',workspace_id=2,parent_id=None)
    row.parent=foreign;row.parent_id=2
    with pytest.raises(PermissionDeniedError):row.to_dict()


def test_organizer_root_alias_and_guards_match_rest(session,test_workspace,test_folder):
    root=test_folder.parent_id;workspace=test_workspace.id;folder=test_folder.id
    file=File(name='file',file_path='object',workspace_id=workspace,parent_id=folder)
    session.add(file);session.commit();file_id=file.id
    # The seam supplies the SQL session; these are actual mutations and checks.
    with patch.object(tools,'get_session',return_value=session),patch.object(tools,'clear_workspace_cache'):
        result=tools.create_folder.invoke({'name':'organized','parent_id':0,'workspace_id':workspace})
        assert '已创建' in result
        assert session.query(Folder).filter_by(name='organized').one().parent_id == root
        assert '移动' in tools.move_folder.invoke({'folder_id':folder,'target_folder_id':0,'workspace_id':workspace})
        assert session.get(Folder,folder).parent_id == root
        assert '移动' in tools.move_file.invoke({'file_id':file_id,'target_folder_id':0,'workspace_id':workspace})
        assert session.get(File,file_id).parent_id == root
        for tool,arguments in [(tools.rename_folder,{'folder_id':root,'new_name':'bad'}),
                (tools.delete_folder,{'folder_id':root}),
                (tools.merge_folders,{'source_folder_id':root,'target_folder_id':folder})]:
            assert '失败' in tool.invoke({**arguments,'workspace_id':workspace})
        assert session.get(Folder,root) is not None
