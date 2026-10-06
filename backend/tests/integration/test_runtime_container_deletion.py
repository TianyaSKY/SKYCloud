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
                        '--label',f'skycloud.runtime_id={runtime}',os.getenv('SKYCLOUD_TEST_PROCESS_IMAGE',os.environ['SKYCLOUD_TEST_RUNTIME_IMAGE']))
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


def test_generation_prepare_preserves_old_runtime_and_replays_stopped_identity(generation_graph,infrastructure):
    graph = generation_graph
    generation = uuid.uuid4().hex
    name = f"skycloud-opencode-w{graph['id']}-u{graph['id']}-g{generation}"
    infrastructure['cleanup'].append(name)
    prepared = graph['client'].prepare(graph['id'],graph['id'],graph['id'],generation)
    assert prepared.id != graph['containers'][graph['id']]
    assert prepared.status == 'created'
    assert all(exists(graph,runtime) for runtime in graph['containers'])
    replay = graph['client'].prepare(graph['id'],graph['id'],graph['id'],generation)
    assert replay.id == prepared.id and replay.status == 'created'
    rows = graph['client'].user_snapshot(graph['id'])
    assert {row['id'] for row in rows if row['runtime_id'] == graph['id']} == {prepared.id,graph['containers'][graph['id']]}
    with graph['db'].connect() as db:
        assert db.scalar(text('SELECT container_id FROM opencode_runtimes WHERE id=:id'),{'id':graph['id']}) == graph['containers'][graph['id']]
    from runtime_controller.client import ControllerError
    with pytest.raises(ControllerError): graph['client'].prepare(graph['id'],graph['id'],graph['id']+9,generation)
    prepared.remove()
    assert all(exists(graph,runtime) for runtime in graph['containers'])


def test_overlapping_generation_prepares_create_only_one_stopped_container(generation_graph,infrastructure):
    graph = generation_graph
    generation = uuid.uuid4().hex
    name = f"skycloud-opencode-w{graph['id']}-u{graph['id']}-g{generation}"
    infrastructure['cleanup'].append(name)
    barrier = Barrier(2)
    def invoke(_):
        barrier.wait(timeout=10)
        return ControllerClient().prepare(graph['id'],graph['id'],graph['id'],generation)
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(invoke,range(2)))
    assert rows[0].id == rows[1].id and rows[0].status == rows[1].status == 'created'
    assert all(exists(graph,runtime) for runtime in graph['containers'])
    rows[0].remove()


@pytest.fixture
def generation_graph(request,monkeypatch):
    import json
    # Redis declares an anonymous /data volume, incompatible with runtime
    # policy. Prepare uses our volume-free Controller image without starting
    # its process; pre-existing fixture processes continue to run Redis.
    info = json.loads(docker('image','inspect',os.getenv('SKYCLOUD_TEST_GENERATION_IMAGE',os.getenv('SKYCLOUD_TEST_CONTROLLER_IMAGE','skycloud-runtime-controller:remediation-test'))).stdout)[0]
    assert not info['Config'].get('Volumes')
    monkeypatch.setenv('SKYCLOUD_TEST_PROCESS_IMAGE',os.environ['SKYCLOUD_TEST_RUNTIME_IMAGE'])
    monkeypatch.setenv('SKYCLOUD_TEST_RUNTIME_IMAGE',info['Id'])
    return request.getfixturevalue('graph')



def test_container_cleanup_uses_database_clock_despite_worker_clock_skew(graph,monkeypatch):
    from datetime import datetime,timezone
    from app.infra import runtime_container_deletion
    monkeypatch.setattr(runtime_container_deletion,'utc_now',lambda:datetime(2000,1,1,tzinfo=timezone.utc))
    with graph['db'].begin() as db:
        db.execute(text('DELETE FROM opencode_runtimes WHERE id=:id'),{'id':graph['id']})
    assert cleanup(graph) == 1 and not exists(graph,graph['id'])
    with graph['db'].connect() as db:
        assert db.scalar(text("SELECT completed_at > now()-interval '1 minute' FROM runtime_container_deletions"))


def test_retired_generation_survives_controller_restart_and_rejects_late_prepare(generation_graph,infrastructure,controller,monkeypatch):
    from runtime_controller.client import ControllerError
    graph = generation_graph
    generation = uuid.uuid4().hex
    name = f"skycloud-opencode-w{graph['id']}-u{graph['id']}-g{generation}"
    infrastructure['cleanup'].append(name)
    prepared = graph['client'].prepare(graph['id'],graph['id'],graph['id'],generation)
    prepared.remove()
    docker('restart',controller['name'])
    port = docker('port',controller['name'],'5003/tcp').stdout.strip().rsplit(':',1)[1]
    controller['url'] = 'http://127.0.0.1:'+port
    monkeypatch.setenv('RUNTIME_CONTROLLER_URL',controller['url'])
    import httpx,time
    deadline = time.monotonic()+15
    while time.monotonic()<deadline:
        try:
            if httpx.get(controller['url']+'/health',headers={'X-Runtime-Controller-Token':controller['token']},timeout=1).status_code == 200: break
        except httpx.HTTPError: pass
        time.sleep(0.1)
    else: raise AssertionError('Controller restart did not recover: '+docker('logs',controller['name'],check=False).stderr)
    with pytest.raises(ControllerError): graph['client'].prepare(graph['id'],graph['id'],graph['id'],generation)
    assert docker('inspect',name,check=False).returncode != 0
    assert all(exists(graph,runtime) for runtime in graph['containers'])


def test_retire_before_late_prepare_never_creates_container(generation_graph,infrastructure):
    from runtime_controller.client import ControllerError
    graph = generation_graph
    generation = uuid.uuid4().hex
    name = f"skycloud-opencode-w{graph['id']}-u{graph['id']}-g{generation}"
    infrastructure['cleanup'].append(name)
    graph['client'].retire_generation(graph['id'],graph['id'],graph['id'],generation)
    with pytest.raises(ControllerError): graph['client'].prepare(graph['id'],graph['id'],graph['id'],generation)
    assert docker('inspect',name,check=False).returncode != 0
    assert all(exists(graph,runtime) for runtime in graph['containers'])


def test_parallel_prepare_and_retire_end_with_durable_retirement(generation_graph,infrastructure):
    from runtime_controller.client import ControllerError
    graph = generation_graph
    generation = uuid.uuid4().hex
    name = f"skycloud-opencode-w{graph['id']}-u{graph['id']}-g{generation}"
    infrastructure['cleanup'].append(name)
    barrier = Barrier(2)
    def prepare():
        barrier.wait(timeout=10)
        try: return graph['client'].prepare(graph['id'],graph['id'],graph['id'],generation).id
        except ControllerError: return None
    def retire():
        barrier.wait(timeout=10)
        return graph['client'].retire_generation(graph['id'],graph['id'],graph['id'],generation)
    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks = [pool.submit(prepare),pool.submit(retire)]
        for task in tasks: task.result(timeout=20)
    assert docker('inspect',name,check=False).returncode != 0
    with pytest.raises(ControllerError): graph['client'].prepare(graph['id'],graph['id'],graph['id'],generation)
    assert all(exists(graph,runtime) for runtime in graph['containers'])


def test_prepared_generation_replays_same_identity_after_controller_restart(generation_graph,infrastructure,controller,monkeypatch):
    graph = generation_graph
    generation = uuid.uuid4().hex
    name = f"skycloud-opencode-w{graph['id']}-u{graph['id']}-g{generation}"
    infrastructure['cleanup'].append(name)
    prepared = graph['client'].prepare(graph['id'],graph['id'],graph['id'],generation)
    docker('restart',controller['name'])
    port = docker('port',controller['name'],'5003/tcp').stdout.strip().rsplit(':',1)[1]
    controller['url'] = 'http://127.0.0.1:'+port
    monkeypatch.setenv('RUNTIME_CONTROLLER_URL',controller['url'])
    import httpx,time
    deadline = time.monotonic()+15
    while time.monotonic()<deadline:
        try:
            if httpx.get(controller['url']+'/health',headers={'X-Runtime-Controller-Token':controller['token']},timeout=1).status_code == 200: break
        except httpx.HTTPError: pass
        time.sleep(0.1)
    else: raise AssertionError('Controller restart did not recover')
    replay = graph['client'].prepare(graph['id'],graph['id'],graph['id'],generation)
    assert replay.id == prepared.id and replay.status == 'created'
    assert all(exists(graph,runtime) for runtime in graph['containers'])
    replay.remove()


def test_durable_candidate_survives_sql_switch_failure_and_can_be_retired(generation_graph,infrastructure):
    from app.features.workspace import runtime_operation_repository as repository
    from app.infra.runtime_operation_cleanup import retire_cancelled_operations
    from app.models.runtime_operation import RuntimeOperation
    graph = generation_graph
    with graph['factory']() as session:
        operation = repository.admit(session,graph['id'],graph['id'])
        generation = operation.generation
        session.commit()
    infrastructure['cleanup'].append(f"skycloud-opencode-w{graph['id']}-u{graph['id']}-g{generation}")
    prepared = graph['client'].prepare(graph['id'],graph['id'],graph['id'],generation)
    with graph['factory']() as session:
        repository.record_candidate(session,generation,prepared.id)
        session.commit()
    with graph['db'].begin() as db:
        db.execute(text("""CREATE FUNCTION reject_operation_switch() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'injected operation switch'; END $$"""))
        db.execute(text("CREATE TRIGGER reject_operation_switch BEFORE UPDATE OF container_id ON opencode_runtimes FOR EACH ROW EXECUTE FUNCTION reject_operation_switch()"))
    with graph['factory']() as session:
        with pytest.raises(Exception,match='injected operation switch'):
            repository.switch_candidate(session,generation)
            session.commit()
        session.rollback()
        operation = session.get(RuntimeOperation,generation)
        assert operation.state == 'preparing' and operation.candidate_container_id == prepared.id
        repository.cancel_runtime(session,graph['id'])
        session.commit()
    with graph['db'].connect() as db:
        assert db.scalar(text('SELECT container_id FROM opencode_runtimes WHERE id=:id'),{'id':graph['id']}) == graph['containers'][graph['id']]
        assert db.scalar(text('SELECT count(*) FROM runtime_container_deletions')) == 0
    assert graph['client'].get(prepared.id).status == 'created'
    assert all(exists(graph,runtime) for runtime in graph['containers'])
    assert retire_cancelled_operations(session_factory=graph['factory']) == 1
    with pytest.raises(ControllerNotFound): graph['client'].get(prepared.id)
    assert all(exists(graph,runtime) for runtime in graph['containers'])


def test_member_demotion_cancels_generation_atomically_and_worker_retires(generation_graph,infrastructure):
    from app.features.workspace import runtime_operation_repository as repository
    from app.infra.runtime_operation_cleanup import retire_cancelled_operations
    from app.models.runtime_operation import RuntimeOperation
    from runtime_controller.client import ControllerError
    graph = generation_graph
    with graph['factory']() as session:
        operation = repository.admit(session,graph['id'],graph['id'])
        generation = operation.generation
        session.commit()
    infrastructure['cleanup'].append(f"skycloud-opencode-w{graph['id']}-u{graph['id']}-g{generation}")
    prepared = graph['client'].prepare(graph['id'],graph['id'],graph['id'],generation)
    with graph['db'].connect() as db:
        db.execute(text("UPDATE workspace_members SET role='viewer' WHERE workspace_id=:id AND user_id=:id"),{'id':graph['id']})
        assert db.scalar(text('SELECT state FROM runtime_operations')) == 'cancelled'
        db.rollback()
        assert db.scalar(text('SELECT state FROM runtime_operations')) == 'preparing'
    assert retire_cancelled_operations(session_factory=graph['factory']) == 0
    with graph['db'].begin() as db:
        db.execute(text("UPDATE workspace_members SET role='viewer' WHERE workspace_id=:id AND user_id=:id"),{'id':graph['id']})
    assert retire_cancelled_operations(session_factory=graph['factory']) == 1
    with pytest.raises(ControllerNotFound): graph['client'].get(prepared.id)
    with pytest.raises(ControllerError): graph['client'].prepare(graph['id'],graph['id'],graph['id'],generation)
    assert all(exists(graph,runtime) for runtime in graph['containers'])


def test_owner_cascade_retains_cancelled_generation_without_foreign_keys(generation_graph,infrastructure):
    from app.features.workspace import runtime_operation_repository as repository
    from app.infra.runtime_operation_cleanup import retire_cancelled_operations
    graph = generation_graph
    with graph['factory']() as session:
        operation = repository.admit(session,graph['id'],graph['id'])
        generation = operation.generation
        session.commit()
    infrastructure['cleanup'].append(f"skycloud-opencode-w{graph['id']}-u{graph['id']}-g{generation}")
    prepared = graph['client'].prepare(graph['id'],graph['id'],graph['id'],generation)
    delete_account(graph)
    with graph['db'].connect() as db:
        assert db.execute(text('SELECT generation,state,finished_at FROM runtime_operations')).one() == (generation,'cancelled',None)
    assert retire_cancelled_operations(session_factory=graph['factory']) == 1
    with pytest.raises(ControllerNotFound): graph['client'].get(prepared.id)
    assert cleanup(graph) == 3
    assert exists(graph,graph['other'])


def test_parallel_sql_admission_commits_only_one_replayable_generation(graph):
    from app.features.workspace import runtime_operation_repository as repository
    barrier = Barrier(2)
    def admit(_):
        barrier.wait(timeout=10)
        with graph['factory']() as session:
            generation = repository.admit(session,graph['id'],graph['id']).generation
            session.commit()
            return generation
    with ThreadPoolExecutor(max_workers=2) as pool:
        generations = list(pool.map(admit,range(2)))
    assert generations[0] == generations[1]
    with graph['db'].connect() as db:
        assert db.scalar(text('SELECT count(*) FROM runtime_operations')) == 1
    assert all(exists(graph,runtime) for runtime in graph['containers'])


def test_committed_candidate_switch_captures_only_old_identity_and_stop_cancels(generation_graph,infrastructure):
    from app.features.workspace import runtime_operation_repository as repository
    from app.infra.runtime_operation_cleanup import retire_cancelled_operations
    graph = generation_graph
    with graph['factory']() as session:
        operation = repository.admit(session,graph['id'],graph['id'])
        generation = operation.generation
        session.commit()
    infrastructure['cleanup'].append(f"skycloud-opencode-w{graph['id']}-u{graph['id']}-g{generation}")
    prepared = graph['client'].prepare(graph['id'],graph['id'],graph['id'],generation)
    with graph['factory']() as session:
        repository.record_candidate(session,generation,prepared.id)
        session.commit()
        repository.switch_candidate(session,generation)
        session.commit()
    with graph['db'].connect() as db:
        assert db.scalar(text('SELECT state FROM runtime_operations')) == 'switched'
        assert db.execute(text('SELECT container_id FROM runtime_container_deletions')).scalars().all() == [graph['containers'][graph['id']]]
    assert cleanup(graph) == 1 and not exists(graph,graph['id'])
    assert graph['client'].get(prepared.id).status == 'created'
    with graph['db'].begin() as db:
        db.execute(text("UPDATE opencode_runtimes SET status='stopped' WHERE id=:id"),{'id':graph['id']})
        assert db.scalar(text('SELECT state FROM runtime_operations')) == 'cancelled'
    assert retire_cancelled_operations(session_factory=graph['factory']) == 1
    with pytest.raises(ControllerNotFound): graph['client'].get(prepared.id)
    assert all(exists(graph,runtime) for runtime in graph['containers'] if runtime != graph['id'])


def test_generation_retirement_sql_marker_failure_replays_durable_fence(generation_graph,infrastructure):
    from app.features.workspace import runtime_operation_repository as repository
    from app.infra.runtime_operation_cleanup import retire_cancelled_operations
    graph = generation_graph
    with graph['factory']() as session:
        operation = repository.admit(session,graph['id'],graph['id'])
        generation = operation.generation
        session.commit()
    infrastructure['cleanup'].append(f"skycloud-opencode-w{graph['id']}-u{graph['id']}-g{generation}")
    prepared = graph['client'].prepare(graph['id'],graph['id'],graph['id'],generation)
    with graph['factory']() as session:
        repository.cancel_runtime(session,graph['id'])
        session.commit()
    with graph['db'].begin() as db:
        db.execute(text("""CREATE FUNCTION reject_retirement_marker() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'injected retirement marker'; END $$"""))
        db.execute(text("CREATE TRIGGER reject_retirement_marker BEFORE UPDATE OF finished_at ON runtime_operations FOR EACH ROW WHEN (NEW.finished_at IS NOT NULL) EXECUTE FUNCTION reject_retirement_marker()"))
    with pytest.raises(Exception,match='injected retirement marker'):
        retire_cancelled_operations(session_factory=graph['factory'])
    with pytest.raises(ControllerNotFound): graph['client'].get(prepared.id)
    with graph['db'].begin() as db:
        assert db.scalar(text('SELECT finished_at FROM runtime_operations')) is None
        db.execute(text('DROP TRIGGER reject_retirement_marker ON runtime_operations'))
    assert retire_cancelled_operations(session_factory=graph['factory']) == 1
    assert all(exists(graph,runtime) for runtime in graph['containers'])


def test_production_start_sql_failure_preserves_old_and_durable_candidate(generation_graph,infrastructure):
    from app.features.workspace import docker_service
    from app.models.workspace import Workspace
    graph = generation_graph
    with graph['db'].begin() as db:
        db.execute(text("""CREATE FUNCTION reject_start_switch() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'injected production start switch'; END $$"""))
        db.execute(text("CREATE TRIGGER reject_start_switch BEFORE UPDATE OF container_id ON opencode_runtimes FOR EACH ROW EXECUTE FUNCTION reject_start_switch()"))
    with graph['factory']() as session:
        with pytest.raises(Exception,match='injected production start switch'):
            docker_service.start(session,session.get(Workspace,graph['id']),graph['id'])
        session.rollback()
    with graph['db'].connect() as db:
        operation = db.execute(text('SELECT generation,candidate_container_id,state FROM runtime_operations')).one()
        assert operation.state == 'preparing'
        assert db.scalar(text('SELECT container_id FROM opencode_runtimes WHERE id=:id'),{'id':graph['id']}) == graph['containers'][graph['id']]
        assert db.scalar(text('SELECT count(*) FROM runtime_container_deletions')) == 0
    infrastructure['cleanup'].append(f"skycloud-opencode-w{graph['id']}-u{graph['id']}-g{operation.generation}")
    assert graph['client'].get(operation.candidate_container_id).status == 'created'
    assert all(exists(graph,runtime) for runtime in graph['containers'])
    from app.features.workspace import runtime_operation_repository as repository
    from app.infra.runtime_operation_cleanup import retire_cancelled_operations
    with graph['factory']() as session:
        repository.cancel_runtime(session,graph['id'])
        session.commit()
    assert retire_cancelled_operations(session_factory=graph['factory']) == 1
    assert all(exists(graph,runtime) for runtime in graph['containers'])


def test_lifecycle_pg_mutex_survives_sql_commit_and_releases_on_exit(graph):
    from app.features.workspace.runtime_execution import execution
    from app.exceptions import ConflictError
    with graph['factory']() as first:
        with execution(first,graph['id'],graph['id']):
            first.execute(text('SELECT 1'))
            first.commit()
            def try_other():
                with graph['factory']() as other:
                    with pytest.raises(ConflictError):
                        with execution(other,graph['id'],graph['id']): pass
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(try_other).result(timeout=10)
        with graph['factory']() as other:
            with execution(other,graph['id'],graph['id']): pass


@pytest.fixture
def delayed_generation_graph(request,infrastructure,monkeypatch):
    from pathlib import Path
    name = infrastructure['target']+'-daemon-proxy'
    infrastructure['cleanup'].append(name)
    gid = docker('run','--rm','--entrypoint','stat','-v','/var/run/docker.sock:/var/run/docker.sock',
                 os.environ['SKYCLOUD_TEST_INFRA_IMAGE'],'-c','%g','/var/run/docker.sock').stdout.strip()
    code=(Path(__file__).parent/'docker_delayed_proxy.py').read_text()
    docker('run','-d','--name',name,'--network',infrastructure['control'],'--network-alias','delayed-daemon',
           '--read-only','--cap-drop','ALL','--security-opt','no-new-privileges:true','--group-add',gid,
           '--tmpfs','/tmp:rw,nosuid,nodev,size=1m,mode=1777','-v','/var/run/docker.sock:/var/run/docker.sock',
           '--entrypoint','python',os.environ['SKYCLOUD_TEST_CONTROLLER_IMAGE'],'-u','-c',code)
    monkeypatch.setenv('SKYCLOUD_TEST_DOCKER_PROXY_HOST','tcp://delayed-daemon:2375')
    graph=request.getfixturevalue('generation_graph')
    graph['proxy']=name
    return graph


def test_killed_controller_retirement_collects_create_delayed_until_after_fence(delayed_generation_graph,infrastructure,controller,monkeypatch):
    import httpx,re,time
    from runtime_controller.client import ControllerError
    graph=delayed_generation_graph
    generation=uuid.uuid4().hex
    name=f"skycloud-opencode-w{graph['id']}-u{graph['id']}-g{generation}"
    infrastructure['cleanup'].append(name)
    with ThreadPoolExecutor(max_workers=1) as pool:
        preparing=pool.submit(graph['client'].prepare,graph['id'],graph['id'],graph['id'],generation)
        deadline=time.monotonic()+10
        while 'CREATE_HELD' not in docker('logs',graph['proxy']).stdout:
            assert time.monotonic()<deadline
            time.sleep(0.1)
        docker('kill','--signal','KILL',controller['name'])
        with pytest.raises(ControllerError): preparing.result(timeout=10)
        docker('start',controller['name'])
        port=docker('port',controller['name'],'5003/tcp').stdout.strip().rsplit(':',1)[1]
        controller['url']='http://127.0.0.1:'+port
        monkeypatch.setenv('RUNTIME_CONTROLLER_URL',controller['url'])
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            try:
                if httpx.get(controller['url']+'/health',headers={'X-Runtime-Controller-Token':controller['token']},timeout=1).status_code==200: break
            except httpx.HTTPError: pass
            time.sleep(0.1)
        else: raise AssertionError('Killed Controller failed to recover')
        # The accepted downstream create is still held and physically absent.
        graph['client'].retire_generation(graph['id'],graph['id'],graph['id'],generation)
        assert docker('inspect',name,check=False).returncode!=0
        docker('exec',graph['proxy'],'python','-c',"from pathlib import Path; Path('/tmp/release-create').write_text('release')")
        deadline=time.monotonic()+15
        identity=None
        while time.monotonic()<deadline:
            completed=re.search(r'CREATE_COMPLETED:([0-9a-f]{64})',docker('logs',graph['proxy']).stdout)
            if completed:
                identity=completed.group(1)
                if docker('inspect',identity,check=False).returncode!=0: break
            time.sleep(0.1)
        assert identity is not None,'Delayed request never created a real Docker resource'
        assert docker('inspect',identity,check=False).returncode!=0,'Retired late resource was not reconciled'
    with pytest.raises(ControllerError): graph['client'].prepare(graph['id'],graph['id'],graph['id'],generation)
    assert all(exists(graph,runtime) for runtime in graph['containers'])


def test_missing_ready_candidate_reports_exact_identity_without_recreating(generation_graph,infrastructure):
    from runtime_controller.client import ControllerGenerationMissing
    graph=generation_graph
    generation=uuid.uuid4().hex
    name=f"skycloud-opencode-w{graph['id']}-u{graph['id']}-g{generation}"
    infrastructure['cleanup'].append(name)
    prepared=graph['client'].prepare(graph['id'],graph['id'],graph['id'],generation)
    docker('rm','-f',prepared.id)  # External disappearance bypasses retirement.
    with pytest.raises(ControllerGenerationMissing) as error:
        graph['client'].prepare(graph['id'],graph['id'],graph['id'],generation)
    assert error.value.generation==generation and error.value.container_id==prepared.id
    assert docker('inspect',name,check=False).returncode!=0
    assert all(exists(graph,runtime) for runtime in graph['containers'])


def test_detached_completed_generation_is_fenced_even_when_physical_id_already_missing(generation_graph,infrastructure):
    from app.features.workspace import runtime_operation_repository as operations
    from app.models.runtime_operation import RuntimeOperation
    graph=generation_graph
    with graph['factory']() as session:
        operation=operations.admit(session,graph['id'],graph['id'])
        generation=operation.generation
        session.commit()
    name=f"skycloud-opencode-w{graph['id']}-u{graph['id']}-g{generation}"
    infrastructure['cleanup'].append(name)
    prepared=graph['client'].prepare(graph['id'],graph['id'],graph['id'],generation)
    with graph['factory']() as session:
        operations.record_candidate(session,generation,prepared.id); session.commit()
        operations.switch_candidate(session,generation); session.commit()
        # Configuration isn't exercised here: mark the lifecycle as completed
        # so this tests ID-queue retirement independent of active cancellation.
        operation=session.get(RuntimeOperation,generation)
        operation.state='complete'
        from app.infra.datetime_utils import utc_now
        operation.finished_at=utc_now()
        session.commit()
    assert cleanup(graph)==1 and not exists(graph,graph['id'])
    docker('rm','-f',prepared.id)
    with graph['db'].begin() as db:
        db.execute(text('UPDATE opencode_runtimes SET container_id=NULL WHERE id=:id'),{'id':graph['id']})
    assert cleanup(graph)==1
    from runtime_controller.client import ControllerError
    with pytest.raises(ControllerError): graph['client'].prepare(graph['id'],graph['id'],graph['id'],generation)
    assert all(exists(graph,runtime) for runtime in graph['containers'] if runtime!=graph['id'])
