"""Actual PostgreSQL share admission after membership changes."""
import os
import pytest
from sqlalchemy.orm import sessionmaker
from tests.integration.test_migrations import postgres
from entry.migrate import upgrade
from app.exceptions import PermissionDeniedError
from app.features.share.service import create_share_link
from app.features.workspace.service import create_workspace
from app.models.user import User
from app.models.file import File
from app.models.share import Share
from app.models.workspace import WorkspaceMember

pytestmark=pytest.mark.skipif(os.getenv('RUN_SHARE_ACCESS_TESTS')!='1',reason='Opt-in isolated PostgreSQL share access')

def test_cached_member_cannot_publish_after_committed_removal(postgres):
    upgrade(postgres)
    factory=sessionmaker(bind=postgres)
    with factory() as seed:
        user=User(username='share-owner',password_hash='isolated-test',role='common')
        viewer=User(username='share-viewer',password_hash='isolated-test',role='common')
        outsider=User(username='share-outsider',password_hash='isolated-test',role='common')
        seed.add_all([user,viewer,outsider]);seed.commit()
        workspace=create_workspace(seed,user.id,'Share access')
        member=WorkspaceMember(workspace_id=workspace.id,user_id=viewer.id,role='viewer')
        row=File(name='shared.txt',file_path='private-key',file_size=2,workspace_id=workspace.id,uploader_id=user.id)
        seed.add_all([member,row]);seed.commit()
        wid,vid,oid,fid,mid=workspace.id,viewer.id,outsider.id,row.id,member.id
    with factory() as reader:
        cached=reader.get(WorkspaceMember,mid)
        assert cached.role=='viewer'
        first=create_share_link(reader,vid,fid)
        assert first.user_id==vid
        with pytest.raises(PermissionDeniedError):create_share_link(reader,oid,fid)
        reader.rollback()
        # Re-load and retain a stale ORM member across another transaction's removal.
        cached=reader.get(WorkspaceMember,mid)
        with factory() as remover:
            remover.query(WorkspaceMember).filter_by(workspace_id=wid,user_id=vid).delete()
            remover.commit()
        assert cached.role=='viewer'
        with pytest.raises(PermissionDeniedError):create_share_link(reader,vid,fid)
        reader.rollback()
        assert reader.query(Share).filter_by(user_id=vid,file_id=fid).count()==1
