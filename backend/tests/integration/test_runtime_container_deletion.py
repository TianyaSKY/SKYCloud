"""Real PostgreSQL/Controller/Docker cleanup; disposable managed Redis containers.

The container process is a fixture, not an OpenCode/model workflow. Controller
identity checks, authenticated RPC and Docker removal are production code.
"""
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.features.auth.user_service import delete_user
from app.infra.runtime_container_deletion import delete_pending_containers
from entry.migrate import upgrade
from runtime_controller.client import ControllerClient, ControllerNotFound
from tests.integration.test_migrations import postgres
from tests.integration.test_runtime_security import infrastructure, controller, docker

pytestmark = pytest.mark.skipif(os.getenv('RUN_CONTAINER_DELETION_TESTS') != '1', reason='Opt-in isolated Controller/Docker cleanup')


@pytest.fixture
def graph(postgres, infrastructure, controller):
    upgrade(postgres)
    identity = int(uuid.uuid4().hex[:6],16) + 1000
    other = identity + 1
    rows = [(identity, identity, identity), (identity+2,identity,other),
            (identity+3,other,identity), (other,other,other)]
    containers = {}
    for runtime,workspace,user in rows:
        name = f'skycloud-opencode-w{workspace}-u{user}'
        infrastructure['cleanup'].append(name)
        result = docker('run','-d','--name',name,'--network',infrastructure['runtime'],
                        '--label','skycloud.component=opencode-runtime',
                        '--label',f'skycloud.workspace_id={workspace}',
                        '--label',f'skycloud.user_id={user}',
                        '--label',f'skycloud.runtime_id={runtime}',os.environ['SKYCLOUD_TEST_RUNTIME_IMAGE'])
        containers[runtime] = result.stdout.strip()
    with postgres.begin() as db:
        db.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (:id,'delete','x','common'),(:other,'keep','x','common')"),{'id':identity,'other':other})
        db.execute(text("INSERT INTO workspaces(id,name,owner_id) VALUES (:id,'delete',:id),(:other,'keep',:other)"),{'id':identity,'other':other})
        db.execute(text("INSERT INTO workspace_members(workspace_id,user_id,role) VALUES (:id,:id,'admin'),(:id,:other,'editor'),(:other,:other,'admin'),(:other,:id,'editor')"),{'id':identity,'other':other})
        for runtime,workspace,user in rows:
            db.execute(text("INSERT INTO opencode_runtimes(id,workspace_id,user_id,container_id,status) VALUES (:id,:workspace,:user,:container,'running')"),{'id':runtime,'workspace':workspace,'user':user,'container':containers[runtime]})
            db.execute(text("INSERT INTO mcp_runtime_tokens(runtime_id,workspace_id,user_id,jti,token_hash,token_preview,expires_at) VALUES (:id,:workspace,:user,:jti,:jti,'p',now()+interval '1 day')"),{'id':runtime,'workspace':workspace,'user':user,'jti':str(runtime)})
    return {'db':postgres,'factory':sessionmaker(bind=postgres),'id':identity,'other':other,'containers':containers,'client':controller['client'],'token':controller['token']}


def delete_account(graph):
    with graph['factory']() as session:
        delete_user(session,graph['id'])


def exists(graph, runtime):
    try:
        graph['client'].get(graph['containers'][runtime])
        return True
    except ControllerNotFound:
        return False


def cleanup(graph, **kwargs):
    return delete_pending_containers(session_factory=graph['factory'],**kwargs)


def test_account_commit_then_controller_refusal_and_retry_removes_exact_resources(graph,monkeypatch):
    delete_account(graph)
    assert all(exists(graph,runtime) for runtime in graph['containers'])
    with graph['db'].connect() as db:
        assert db.scalar(text('SELECT count(*) FROM runtime_container_deletions')) == 3
        assert db.execute(text('SELECT user_id FROM mcp_runtime_tokens')).scalars().all() == [graph['other']]
    monkeypatch.setenv('RUNTIME_CONTROLLER_TOKEN','wrong-'+'x'*40)
    assert cleanup(graph) == 0
    with graph['db'].begin() as db:
        assert db.execute(text('SELECT attempts,last_error_type FROM runtime_container_deletions')).all() == [(1,'ControllerError')]*3
        db.execute(text('UPDATE runtime_container_deletions SET next_attempt_at=now()'))
    monkeypatch.setenv('RUNTIME_CONTROLLER_TOKEN',graph['token'])
    assert cleanup(graph) == 3
    for runtime in graph['containers']:
        assert exists(graph,runtime) == (runtime == graph['other'])


def test_account_sql_commit_failure_never_removes_containers_or_keeps_intents(graph):
    with graph['db'].begin() as db:
        db.execute(text("""CREATE FUNCTION reject_account_commit() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'injected account rollback'; END $$"""))
        db.execute(text("""CREATE CONSTRAINT TRIGGER reject_account_commit AFTER DELETE ON users
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION reject_account_commit()"""))
    with graph['factory']() as session:
        with pytest.raises(Exception,match='injected account rollback'):
            delete_user(session,graph['id'])
        session.rollback()
    assert cleanup(graph) == 0
    assert all(exists(graph,runtime) for runtime in graph['containers'])
    with graph['db'].connect() as db:
        assert db.scalar(text('SELECT count(*) FROM opencode_runtimes')) == 4
        assert db.scalar(text('SELECT count(*) FROM runtime_container_deletions')) == 0
        assert db.scalar(text('SELECT count(*) FROM mcp_runtime_tokens WHERE revoked_at IS NULL')) == 4


def test_actual_removal_followed_by_sql_marker_failure_replays_not_found(graph):
    target = graph['id']
    with graph['db'].begin() as db:
        db.execute(text('DELETE FROM opencode_runtimes WHERE id=:id'),{'id':target})
        db.execute(text("""CREATE FUNCTION reject_cleanup_marker() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'injected marker failure'; END $$"""))
        db.execute(text("""CREATE TRIGGER reject_cleanup_marker BEFORE UPDATE ON runtime_container_deletions
            FOR EACH ROW WHEN (NEW.completed_at IS NOT NULL) EXECUTE FUNCTION reject_cleanup_marker()"""))
    with pytest.raises(Exception,match='injected marker failure'):
        cleanup(graph)
    assert not exists(graph,target)
    with graph['db'].begin() as db:
        assert db.scalar(text('SELECT completed_at FROM runtime_container_deletions')) is None
        db.execute(text('DROP TRIGGER reject_cleanup_marker ON runtime_container_deletions'))
    assert cleanup(graph) == 1
    assert all(exists(graph,runtime) for runtime in graph['containers'] if runtime != target)


def test_overlapping_duplicate_intents_idempotently_remove_one_container(graph):
    target = graph['id']
    with graph['db'].begin() as db:
        db.execute(text('DELETE FROM opencode_runtimes WHERE id=:id'),{'id':target})
        db.execute(text('INSERT INTO runtime_container_deletions(container_id) VALUES (:id)'),{'id':graph['containers'][target]})
    barrier = Barrier(2)
    class Client:
        def __init__(self): self.containers = self
        def get(self, reference):
            remote = ControllerClient().get(reference)
            remove = remote.remove
            def overlap(**kwargs):
                barrier.wait(timeout=10)
                return remove(**kwargs)
            remote.remove = overlap
            return remote
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _:cleanup(graph,controller_factory=Client,limit=1),range(2)))
    assert sorted(results) in ([0,1],[1,1])
    if sum(results) == 1:
        # Docker can reject a second overlapping removal as already in progress.
        # That refusal must remain pending until a 404 replay completes it.
        with graph['db'].begin() as db:
            assert db.execute(text('SELECT attempts,last_error_type FROM runtime_container_deletions WHERE completed_at IS NULL')).all() == [(1,'ControllerError')]
            db.execute(text('UPDATE runtime_container_deletions SET next_attempt_at=now() WHERE completed_at IS NULL'))
        assert cleanup(graph) == 1
    assert not exists(graph,target)
    with graph['db'].connect() as db:
        assert db.scalar(text('SELECT count(*) FROM runtime_container_deletions WHERE completed_at IS NOT NULL')) == 2


def test_current_reference_protects_container_and_later_detach_creates_fresh_intent(graph):
    target = graph['id']
    with graph['db'].begin() as db:
        db.execute(text('INSERT INTO runtime_container_deletions(container_id) VALUES (:id)'),{'id':graph['containers'][target]})
    assert cleanup(graph) == 1 and exists(graph,target)
    with graph['db'].begin() as db:
        db.execute(text('UPDATE opencode_runtimes SET container_id=NULL WHERE id=:id'),{'id':target})
    assert cleanup(graph) == 1 and not exists(graph,target)



def test_workspace_delete_with_loaded_conversations_defers_own_container_cleanup(graph):
    from app.features.workspace.service import delete_workspace
    from app.models.workspace import Workspace
    with graph['db'].begin() as db:
        db.execute(text("INSERT INTO assistant_conversations(workspace_id,user_id,mode) VALUES (:id,:other,'fast'),(:other,:other,'fast')"),{'id':graph['id'],'other':graph['other']})
    with graph['factory']() as session:
        workspace = session.get(Workspace,graph['id'])
        assert workspace.assistant_conversations
        assert workspace.opencode_runtimes
        delete_workspace(session,graph['id'],graph['id'])
    assert all(exists(graph,runtime) for runtime in graph['containers'])
    assert cleanup(graph) == 2
    assert not exists(graph,graph['id']) and not exists(graph,graph['id']+2)
    assert exists(graph,graph['id']+3) and exists(graph,graph['other'])
    with graph['db'].connect() as db:
        assert db.scalar(text('SELECT count(*) FROM users')) == 2
        assert db.execute(text('SELECT workspace_id FROM assistant_conversations')).scalars().all() == [graph['other']]



def test_member_removal_revokes_now_and_cleans_only_members_runtime_after_commit(graph):
    from app.features.workspace.service import remove_member
    target = graph['id'] + 2
    with graph['factory']() as session:
        remove_member(session,graph['id'],graph['id'],graph['other'])
    assert all(exists(graph,runtime) for runtime in graph['containers'])
    with graph['db'].connect() as db:
        assert db.scalar(text('SELECT count(*) FROM workspace_members WHERE workspace_id=:workspace AND user_id=:user'),{'workspace':graph['id'],'user':graph['other']}) == 0
        assert db.scalar(text('SELECT count(*) FROM mcp_runtime_tokens WHERE runtime_id=:id'),{'id':target}) == 0
        assert db.scalar(text('SELECT count(*) FROM runtime_container_deletions')) == 1
        assert db.scalar(text('SELECT count(*) FROM users')) == 2
    assert cleanup(graph) == 1
    for runtime in graph['containers']:
        assert exists(graph,runtime) == (runtime != target)


@pytest.mark.parametrize('legacy_name',['workspace','per_user'])
def test_account_retirement_cleans_known_legacy_format_through_authenticated_controller(postgres,infrastructure,controller,legacy_name):
    upgrade(postgres)
    identity = int(uuid.uuid4().hex[:6],16) + 1000
    name = f'skycloud-workspace-{identity}' if legacy_name == 'workspace' else f'skycloud-opencode-w{identity}-u{identity}'
    infrastructure['cleanup'].append(name)
    ref = docker('run','-d','--name',name,'--network',infrastructure['runtime'],
                 '--label','skycloud.component=opencode-runtime',
                 '--label',f'skycloud.workspace_id={identity}',
                 '--label',f'skycloud.user_id={identity}',
                 '--label','skycloud.runtime_id=',os.environ['SKYCLOUD_TEST_RUNTIME_IMAGE']).stdout.strip()
    with postgres.begin() as db:
        db.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (:id,'legacy','x','common')"),{'id':identity})
        db.execute(text("INSERT INTO workspaces(id,name,owner_id,container_id) VALUES (:id,'legacy',:id,:container)"),{'id':identity,'container':ref})
    factory = sessionmaker(bind=postgres)
    with factory() as session: delete_user(session,identity)
    assert docker('inspect',ref,check=False).returncode == 0
    assert delete_pending_containers(session_factory=factory) == 1
    assert docker('inspect',ref,check=False).returncode != 0
    assert delete_pending_containers(session_factory=factory) == 0
    with postgres.connect() as db:
        assert db.scalar(text('SELECT completed_at FROM runtime_container_deletions')) is not None


def test_cleanup_cannot_remove_unlabeled_infrastructure_even_with_full_id(postgres,infrastructure,controller):
    import json
    upgrade(postgres)
    ref = json.loads(docker('inspect',infrastructure['target']).stdout)[0]['Id']
    with postgres.begin() as db:
        db.execute(text('INSERT INTO runtime_container_deletions(container_id) VALUES (:id)'),{'id':ref})
    assert delete_pending_containers(session_factory=sessionmaker(bind=postgres)) == 0
    assert docker('inspect',ref,check=False).returncode == 0
    with postgres.connect() as db:
        assert db.execute(text('SELECT completed_at,attempts,last_error_type FROM runtime_container_deletions')).all() == [(None,1,'ControllerError')]
