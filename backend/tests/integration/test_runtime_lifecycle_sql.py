# pyright: reportPrivateUsage=false
"""停止/替换身份转换的真实 PostgreSQL 恢复语义测试."""
import os
from types import SimpleNamespace
from typing import Any, Literal, cast

import pytest
from app.exceptions import PermissionDeniedError
from app.features.workspace import docker_service
from app.features.workspace import runtime_operation_repository as repository
from app.features.workspace.runtime_port import RuntimeController
from app.infra.runtime_operations import recover_runtime_operations
from app.models.runtime_operation import RuntimeOperation
from entry.init_db import initialize_database
from runtime_common.contracts import ContainerPayload, UserSnapshotRow
from runtime_common.json_types import JsonObject
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker
from tests.integration.test_database_init import (
    postgres,  # noqa: F401  (fixture re-export)
)

__all__ = ["postgres"]

pytestmark = pytest.mark.skipif(
    os.getenv('RUN_RUNTIME_LIFECYCLE_TESTS') != '1',
    reason='Opt-in isolated PostgreSQL runtime lifecycle recovery',
)

USER = 900001
WORKSPACE = 900001
NEW_CONTAINER = 'b' * 64


@pytest.fixture
def graph(postgres: Any):  # noqa: F811
    initialize_database(postgres)
    with postgres.begin() as db:
        db.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (:id,'lifecycle','x','common')"),
                   {'id': USER})
        db.execute(text("INSERT INTO workspaces(id,name,owner_id) VALUES (:id,'lifecycle',:id)"), {'id': WORKSPACE})
        db.execute(text("INSERT INTO workspace_members(workspace_id,user_id,role) VALUES (:id,:id,'admin')"),
                   {'id': WORKSPACE})
        db.execute(text("INSERT INTO opencode_runtimes(workspace_id,user_id,status) VALUES (:id,:id,'stopped')"),
                   {'id': WORKSPACE})
    return sessionmaker(bind=postgres)


class RecordingController(RuntimeController):
    """显式 Controller RPC 契约的记录实现."""

    events: list[tuple[str, Any]]
    prepare_hook: Any

    def __init__(self, recorder: dict[str, Any]):
        self.events = recorder['events']
        self.prepare_hook = recorder['prepare_hook']

    def inspect(self, ref: str) -> ContainerPayload:
        self.events.append(('inspect', ref))
        return ContainerPayload(
            id=ref,
            name=ref,
            status='created',
            image='',
            labels={},
            ports={},
        )

    def prepare(self, workspace_id: int, user_id: int, runtime_id: int, generation: str) -> ContainerPayload:
        self.events.append(('prepare', generation))
        self.prepare_hook(int(runtime_id))
        return ContainerPayload(
            id=NEW_CONTAINER,
            name=NEW_CONTAINER,
            status='created',
            image='',
            labels={},
            ports={},
        )

    def retire_generation(self, workspace_id: int, user_id: int, runtime_id: int, generation: str) -> None:
        self.events.append(('retire', (workspace_id, user_id, runtime_id, generation)))

    def remove_legacy(self, ref: str) -> None:
        self.events.append(('remove_legacy', ref))

    def user_snapshot(self, user_id: int) -> list[UserSnapshotRow]:
        self.events.append(('user_snapshot', user_id))
        return []

    def action(self, container_id: str, action: Literal['start', 'stop', 'restart', 'remove']) -> None:
        self.events.append((action, container_id))

    def read_config(self, container_id: str) -> JsonObject:
        self.events.append(('read_config', container_id))
        return {}

    def write_config(self, container_id: str, config: JsonObject) -> None:
        self.events.append(('write_config', container_id))


@pytest.fixture
def controller(monkeypatch: Any) -> dict[str, Any]:
    def default_prepare_hook(runtime_id: Any) -> None:
        pass

    def mock_sync_mcp(*args: Any, **kwargs: Any) -> None:
        pass

    recorder: dict[str, Any] = {'events': [], 'prepare_hook': default_prepare_hook}
    monkeypatch.setattr(docker_service, '_client', lambda: RecordingController(recorder))
    monkeypatch.setattr(docker_service, '_sync_mcp_via_runtime_api', mock_sync_mcp)
    return recorder


def admitted(factory: Any) -> str:
    with factory() as session:
        operation = repository.admit(session, WORKSPACE, USER)
        generation = str(operation.generation)
        session.commit()
    return generation


def operation_state(factory: Any, generation: Any) -> str | None:
    with factory() as session:
        op = session.get(RuntimeOperation, generation)
        return op.state if op is not None else None


def runtime_id(postgres: Any) -> int:
    with postgres.connect() as db:
        return int(db.scalar(text("SELECT id FROM opencode_runtimes WHERE workspace_id=:id"), {'id': WORKSPACE}))


def test_stop_cancellation_commits_before_the_rpc_and_survives_lost_commit(graph: Any, controller: Any, postgres: Any) -> None:
    # 在准入前发布运行中容器: 在拥有活跃代系时更新 container_id/status
    # 会 (正确地) 通过 0015 触发器将其取消.
    with postgres.begin() as db:
        db.execute(text("UPDATE opencode_runtimes SET container_id=:container,status='running'"),
                   {'container': 'a' * 64})
    generation = admitted(graph)
    assert operation_state(graph, generation) == 'preparing'

    def crash(*args: Any, **kwargs: Any):
        raise RuntimeError('process lost after the stop RPC')

    controller['crash'] = crash
    import app.mcp.runtime_token_service as runtime_token_service
    original = runtime_token_service.revoke_runtime_tokens
    runtime_token_service.revoke_runtime_tokens = crash
    try:
        with graph() as session:
            with pytest.raises(RuntimeError):
                docker_service.stop(session, cast(Any, SimpleNamespace(id=WORKSPACE)), USER)
    finally:
        runtime_token_service.revoke_runtime_tokens = original

    assert ('stop', 'a' * 64) in controller['events']
    # 停止操作丢失了最终 commit, 因此只有持久化取消能阻止恢复.
    assert operation_state(graph, generation) == 'cancelled'
    assert recover_runtime_operations(session_factory=graph) == 0
    assert operation_state(graph, generation) == 'cancelled'
    assert [event for event in controller['events'] if event[0] in ('prepare', 'start')] == []


def test_losing_edit_permission_during_prepare_retires_generation_and_denies(graph: Any, controller: dict[str, Any], postgres: Any) -> None:
    expected_runtime = runtime_id(postgres)

    def demote(runtime: Any):
        # 并发管理员在准备进行期间降级了调用方;
        # 0015 触发器在同一事务中取消已准入的代系.
        with postgres.begin() as db:
            db.execute(text("UPDATE workspace_members SET role='viewer' WHERE workspace_id=:id AND user_id=:id"),
                       {'id': WORKSPACE})

    controller['prepare_hook'] = demote
    with graph() as session:
        with pytest.raises(PermissionDeniedError):
            docker_service.start(session, cast(Any, SimpleNamespace(id=WORKSPACE)), USER)

    prepared = [event for event in controller['events'] if event[0] == 'prepare']
    retired = [event for event in controller['events'] if event[0] == 'retire']
    assert len(prepared) == 1 and len(retired) == 1
    assert retired[0][1] == (WORKSPACE, USER, expected_runtime, prepared[0][1])
    assert [event for event in controller['events'] if event[0] == 'start'] == []
    assert operation_state(graph, prepared[0][1]) == 'cancelled'
