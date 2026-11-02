"""带 PostgreSQL 范围/角色强制的真实锁定 LangChain 工具调用测试."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from app.models.file import File
from app.models.folder import Folder
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember
from tests.integration.test_migrations import postgres  # noqa: F401
from tests.integration.test_worker_outcomes import file_database  # noqa: F401
from tests.integration.test_runtime_security import infrastructure  # noqa: F401

pytestmark = pytest.mark.skipif(os.getenv("RUN_ORGANIZE_SCOPE_TESTS") != "1", reason="Opt-in isolated organize tool identities")

PROBE = """
import concurrent.futures, json, sys
from app.features.folder.organize import tools
from app.features.folder.organize.scope import OrganizeScope, bind_tools
from app.infra.extensions import SessionLocal
from app.exceptions import PermissionDeniedError
from app.models.workspace import WorkspaceMember
from app.models.folder import Folder
from app.models.file import File
data = json.loads(sys.argv[1])
def bind(ws, user):
    sources = [tools.get_all_files, tools.get_file_information, tools.create_folder, tools.rename_folder, tools.merge_folders]
    return {item.name: item for item in bind_tools(sources, OrganizeScope(ws, user, SessionLocal))}
a = bind(data['ws_a'], data['user_a'])
b = bind(data['ws_b'], data['user_b'])
def denied(tool, args):
    try:
        tool.invoke(args)
    except PermissionDeniedError:
        return
    raise AssertionError('Expected authorization rejection')
denied(tools.get_file_information, {'file_id':data['foreign_file']})
denied(a['get_all_files'], {'workspace_id':data['ws_b']})
info = a['get_file_information'].invoke({'file_id':data['foreign_file']})
assert 'FOREIGN_SECRET' not in info
a['create_folder'].invoke({'name':'bad-parent', 'parent_id':data['foreign_folder'], 'workspace_id':data['ws_a']})
with SessionLocal() as db:
    assert db.query(Folder).filter_by(name='bad-parent').count() == 0
with concurrent.futures.ThreadPoolExecutor(2) as pool:
    first = pool.submit(a['get_all_files'].invoke, {'workspace_id':data['ws_a']})
    second = pool.submit(b['get_all_files'].invoke, {'workspace_id':data['ws_b']})
    assert 'worker.txt' in first.result() and 'FOREIGN_SECRET' not in first.result()
    assert 'FOREIGN_SECRET' in second.result() and 'worker.txt' not in second.result()
a['rename_folder'].invoke({'folder_id':data['own_folder'], 'new_name':'allowed', 'workspace_id':data['ws_a']})
# 历史不一致的父链接绝不能引发递归移动或级联删除.
with SessionLocal() as db:
    db.get(File, data['foreign_file']).parent_id = data['own_folder']
    db.commit()
a['merge_folders'].invoke({'source_folder_id':data['own_folder'], 'target_folder_id':data['target_folder'], 'workspace_id':data['ws_a']})
with SessionLocal() as db:
    assert db.get(File, data['foreign_file']).parent_id == data['own_folder']
    assert db.get(Folder, data['own_folder']) is not None
    db.query(WorkspaceMember).filter_by(workspace_id=data['ws_a'],user_id=data['user_a']).one().role = 'viewer'
    db.commit()
denied(a['rename_folder'], {'folder_id':data['own_folder'],'new_name':'denied','workspace_id':data['ws_a']})
with SessionLocal() as db:
    assert db.get(Folder, data['own_folder']).name == 'allowed'
    db.query(WorkspaceMember).filter_by(workspace_id=data['ws_a'],user_id=data['user_a']).delete()
    db.commit()
denied(a['get_file_information'], {'file_id':data['own_file']})
print('ORGANIZE_SCOPE_VERIFIED')
"""


def test_bound_tools_deny_foreign_scope_and_revoked_membership(file_database, postgres, infrastructure):
    factory, file_id, workspace_id = file_database
    with factory() as db:
        own_user = db.get(File, file_id).uploader_id
        other = User(username="organize-other", password_hash="synthetic-unused", role="common")
        db.add(other); db.flush()
        foreign = Workspace(name="Foreign", owner_id=other.id)
        db.add(foreign); db.flush()
        db.add_all([
            WorkspaceMember(workspace_id=workspace_id, user_id=own_user, role="admin"),
            WorkspaceMember(workspace_id=foreign.id, user_id=other.id, role="admin"),
        ])
        root_a = Folder(name="/", workspace_id=workspace_id, parent_id=None)
        root_b = Folder(name="/", workspace_id=foreign.id, parent_id=None)
        db.add_all([root_a, root_b]); db.flush()
        own_folder = Folder(name="own", workspace_id=workspace_id, parent_id=root_a.id)
        target_folder = Folder(name="target", workspace_id=workspace_id, parent_id=root_a.id)
        foreign_folder = Folder(name="foreign", workspace_id=foreign.id, parent_id=root_b.id)
        db.add_all([own_folder, target_folder, foreign_folder]); db.flush()
        file = File(name="FOREIGN_SECRET", description="FOREIGN_SECRET",
                    file_path="foreign.txt", workspace_id=foreign.id, uploader_id=other.id)
        db.add(file); db.commit()
        data = dict(ws_a=workspace_id, ws_b=foreign.id, user_a=own_user, user_b=other.id,
                    own_file=file_id, foreign_file=file.id, own_folder=own_folder.id,
                    target_folder=target_folder.id, foreign_folder=foreign_folder.id)
    env = {**os.environ, "DATABASE_URL":postgres.url.render_as_string(hide_password=False),
           "REDIS_PORT":str(infrastructure["redis"].connection_pool.connection_kwargs["port"])}
    result = subprocess.run([sys.executable,"-c",PROBE,json.dumps(data)],
        cwd=Path(__file__).resolve().parents[2], env=env, capture_output=True,text=True,timeout=30)
    assert result.returncode == 0, result.stderr[-5000:]
    assert "ORGANIZE_SCOPE_VERIFIED" in result.stdout
