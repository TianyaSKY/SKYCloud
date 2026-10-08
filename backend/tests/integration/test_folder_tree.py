"""Real PostgreSQL ordering across REST/MCP service and scoped organizer writers."""
import concurrent.futures
import os
import threading
import time
import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker
from tests.integration.test_migrations import postgres
from entry.migrate import upgrade
from app.features.folder import repository, service, tree
from app.features.folder.organize import tools
from app.features.folder.organize.scope import OrganizeScope, bind_tools
from app.features.workspace.service import create_workspace
from app.models.user import User
from app.models.folder import Folder
from app.exceptions import BusinessRuleError, ResourceNotFoundError

pytestmark=pytest.mark.skipif(os.getenv('RUN_FOLDER_TREE_TESTS')!='1',reason='Opt-in isolated PostgreSQL folder ordering')

@pytest.fixture
def graph(postgres):
    upgrade(postgres)
    factory=sessionmaker(bind=postgres)
    with factory() as db:
        user=User(username='tree-owner',password_hash='isolated-test-hash',role='common')
        db.add(user);db.commit()
        ws=create_workspace(db,user.id,'Tree ordering')
        root=repository.root_id(db,ws.id)
        a=Folder(name='A',workspace_id=ws.id,parent_id=root)
        b=Folder(name='B',workspace_id=ws.id,parent_id=root)
        db.add_all([a,b]);db.commit()
        ids=dict(user=user.id,workspace=ws.id,root=root,a=a.id,b=b.id)
    return factory,ids,postgres


def assert_waiting(engine, workspace, future):
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        with engine.connect() as conn:
            waiting=conn.scalar(text("SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND classid::bigint=:namespace AND objid::bigint=:workspace AND NOT granted"),
                                {'namespace':repository.TREE_LOCK_NAMESPACE,'workspace':workspace})
        if waiting:
            assert not future.done()
            return
        assert not future.done(), 'writer skipped the tree lock'
        time.sleep(.02)
    raise AssertionError('writer did not wait on the actual PostgreSQL tree lock')


@pytest.mark.parametrize('other_writer',['service','organizer'])
def test_opposite_moves_are_ordered_and_stale_ancestry_cannot_make_cycle(graph,monkeypatch,other_writer):
    factory,ids,engine=graph
    entered,release=threading.Event(),threading.Event()
    original=tree.validate_parent
    def pause(*args,**kwargs):
        parent=original(*args,**kwargs)
        if kwargs.get('moving_id')==ids['a']:
            entered.set();assert release.wait(10)
        return parent
    monkeypatch.setattr(tree,'validate_parent',pause)
    def first():
        with factory() as db:
            service.update_folder(db,ids['a'],{'parent_id':ids['b']})
    def cached_session():
        db=factory()
        # Keep the ORM objects alive to prove the identity map is actually stale.
        db.info['cached']=[db.get(Folder,ids['a']),db.get(Folder,ids['b'])]
        return db
    def second():
        if other_writer=='organizer':
            scope=OrganizeScope(ids['workspace'],ids['user'],cached_session)
            move=bind_tools([tools.move_folder],scope)[0]
            return move.invoke({'folder_id':ids['b'],'target_folder_id':ids['a'],'workspace_id':ids['workspace']})
        with cached_session() as db:
            with pytest.raises(BusinessRuleError):
                service.update_folder(db,ids['b'],{'parent_id':ids['a']})
            return 'rejected'
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        owner=pool.submit(first)
        waiter=None
        try:
            assert entered.wait(5)
            waiter=pool.submit(second)
            assert_waiting(engine,ids['workspace'],waiter)
        finally:
            release.set()
        owner.result(timeout=10)
        outcome=waiter.result(timeout=10)
    assert ('失败' in outcome) if other_writer=='organizer' else outcome=='rejected'
    with factory() as db:
        assert db.get(Folder,ids['a']).parent_id==ids['b']
        assert db.get(Folder,ids['b']).parent_id==ids['root']
        assert db.get(Folder,ids['a']).to_dict()['path']=='/B/A'


def test_deleted_parent_tree_cannot_be_resurrected_from_cached_child(graph,monkeypatch):
    factory,ids,engine=graph
    with factory() as db:
        child=Folder(name='child',workspace_id=ids['workspace'],parent_id=ids['a'])
        db.add(child);db.commit();child_id=child.id
    entered,release=threading.Event(),threading.Event()
    original=repository.lock_tree
    def pause(db,workspace):
        original(db,workspace)
        if db.info.get('deleting'):
            entered.set();assert release.wait(10)
    monkeypatch.setattr(repository,'lock_tree',pause)
    def remove():
        with factory() as db:
            db.info['deleting']=True
            service.delete_folder(db,ids['a'])
    def move_cached():
        with factory() as db:
            cached=db.get(Folder,child_id)
            assert cached.parent_id==ids['a']
            with pytest.raises(ResourceNotFoundError):
                service.update_folder(db,child_id,{'parent_id':ids['b']})
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        owner=pool.submit(remove)
        waiter=None
        try:
            assert entered.wait(5)
            waiter=pool.submit(move_cached)
            assert_waiting(engine,ids['workspace'],waiter)
        finally:
            release.set()
        owner.result(timeout=10);waiter.result(timeout=10)
    with factory() as db:
        assert db.get(Folder,ids['a']) is None and db.get(Folder,child_id) is None
        assert db.get(Folder,ids['b']).parent_id==ids['root']
