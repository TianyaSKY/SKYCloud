"""Real PostgreSQL recovery semantics for stop/replace identity transitions.

The Controller is a recording seam: the assertions are about committed SQL state,
the database triggers that cancel generations, and the recovery worker's decision,
not about Docker.
"""
import os
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.exceptions import PermissionDeniedError
from app.features.workspace import docker_service, runtime_operation_repository as repository
from app.infra.runtime_operations import recover_runtime_operations
from app.models.runtime_operation import RuntimeOperation
from entry.migrate import upgrade
from tests.integration.test_migrations import postgres  # noqa: F401  (fixture re-export)

pytestmark = pytest.mark.skipif(
    os.getenv('RUN_RUNTIME_LIFECYCLE_TESTS') != '1',
    reason='Opt-in isolated PostgreSQL runtime lifecycle recovery',
)

USER = 900001
WORKSPACE = 900001
NEW_CONTAINER = 'b' * 64


@pytest.fixture
def graph(postgres):  # noqa: F811
    upgrade(postgres)
    with postgres.begin() as db:
        db.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (:id,'lifecycle','x','common')"),
                   {'id': USER})
        db.execute(text("INSERT INTO workspaces(id,name,owner_id) VALUES (:id,'lifecycle',:id)"), {'id': WORKSPACE})
        db.execute(text("INSERT INTO workspace_members(workspace_id,user_id,role) VALUES (:id,:id,'admin')"),
                   {'id': WORKSPACE})
        db.execute(text("INSERT INTO opencode_runtimes(workspace_id,user_id,status) VALUES (:id,:id,'stopped')"),
                   {'id': WORKSPACE})
    return sessionmaker(bind=postgres)


class RecordingController:
    """Controller seam: records lifecycle RPCs and runs a per-test prepare hook."""

    def __init__(self, recorder):
        self.events = recorder['events']
        self.prepare_hook = recorder['prepare_hook']
        self.containers = SimpleNamespace(get=self._container)

    def _container(self, identity):
        container = SimpleNamespace(id=identity)
        container.stop = lambda timeout=None: self.events.append(('stop', timeout))
        container.start = lambda: self.events.append(('start', identity))
        return container

    def prepare(self, workspace_id, user_id, runtime_id, generation):
        self.events.append(('prepare', generation))
        self.prepare_hook(int(runtime_id))
        return SimpleNamespace(id=NEW_CONTAINER)

    def retire_generation(self, workspace_id, user_id, runtime_id, generation):
        self.events.append(('retire', (workspace_id, user_id, runtime_id, generation)))


@pytest.fixture
def controller(monkeypatch):
    recorder = {'events': [], 'prepare_hook': lambda runtime_id: None}
    monkeypatch.setattr(docker_service, '_client', lambda: RecordingController(recorder))
    monkeypatch.setattr(docker_service, '_sync_mcp_via_runtime_api', lambda *args, **kwargs: None)
    return recorder


def admitted(factory):
    with factory() as session:
        operation = repository.admit(session, WORKSPACE, USER)
        generation = operation.generation
        session.commit()
    return generation


def operation_state(factory, generation):
    with factory() as session:
        return session.get(RuntimeOperation, generation).state


def runtime_id(postgres):
    with postgres.connect() as db:
        return db.scalar(text("SELECT id FROM opencode_runtimes WHERE workspace_id=:id"), {'id': WORKSPACE})


def test_stop_cancellation_commits_before_the_rpc_and_survives_lost_commit(graph, controller, postgres):
    # Publish the running container before admission: updating container_id/status with an
    # active generation would (correctly) cancel it through the 0015 trigger.
    with postgres.begin() as db:
        db.execute(text("UPDATE opencode_runtimes SET container_id=:container,status='running'"),
                   {'container': 'a' * 64})
    generation = admitted(graph)
    assert operation_state(graph, generation) == 'preparing'

    def crash(*args, **kwargs):
        raise RuntimeError('process lost after the stop RPC')

    controller['crash'] = crash
    import app.mcp.runtime_token_service as runtime_token_service
    original = runtime_token_service.revoke_runtime_tokens
    runtime_token_service.revoke_runtime_tokens = crash
    try:
        with graph() as session:
            with pytest.raises(RuntimeError):
                docker_service.stop(session, SimpleNamespace(id=WORKSPACE), USER)
    finally:
        runtime_token_service.revoke_runtime_tokens = original

    assert ('stop', 10) in controller['events']
    # The stop lost its final commit, so only the durable cancellation can prevent recovery.
    assert operation_state(graph, generation) == 'cancelled'
    assert recover_runtime_operations(session_factory=graph) == 0
    assert operation_state(graph, generation) == 'cancelled'
    assert [event for event in controller['events'] if event[0] in ('prepare', 'start')] == []


def test_losing_edit_permission_during_prepare_retires_generation_and_denies(graph, controller, postgres):
    expected_runtime = runtime_id(postgres)

    def demote(runtime):
        # A concurrent administrator demotes the caller while prepare is in flight;
        # the 0015 trigger cancels the admitted generation in the same transaction.
        with postgres.begin() as db:
            db.execute(text("UPDATE workspace_members SET role='viewer' WHERE workspace_id=:id AND user_id=:id"),
                       {'id': WORKSPACE})

    controller['prepare_hook'] = demote
    with graph() as session:
        with pytest.raises(PermissionDeniedError):
            docker_service.start(session, SimpleNamespace(id=WORKSPACE), USER)

    prepared = [event for event in controller['events'] if event[0] == 'prepare']
    retired = [event for event in controller['events'] if event[0] == 'retire']
    assert len(prepared) == 1 and len(retired) == 1
    assert retired[0][1] == (WORKSPACE, USER, expected_runtime, prepared[0][1])
    assert [event for event in controller['events'] if event[0] == 'start'] == []
    assert operation_state(graph, prepared[0][1]) == 'cancelled'
