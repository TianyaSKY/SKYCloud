"""实际 SQL 准入/切换/回滚与持久退役契约测试."""
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.exceptions import ConflictError, PermissionDeniedError
from app.features.workspace import runtime_operation_repository as repository
from app.infra.datetime_utils import utc_now
from app.infra.runtime_operation_cleanup import retire_cancelled_operations
from app.models.opencode_runtime import OpenCodeRuntime
from app.models.runtime_operation import RuntimeOperation
from app.models.runtime_container_deletion import RuntimeContainerDeletion
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember


def test_candidate_switch_rollback_keeps_prior_identity_and_candidate_intent(
    session: Session, test_workspace: Workspace, test_user: User
) -> None:
    runtime = OpenCodeRuntime(workspace_id=test_workspace.id, user_id=test_user.id, container_id='a' * 64, status='running')
    session.add(runtime)
    session.commit()
    operation = repository.admit(session, test_workspace.id, test_user.id)
    generation = operation.generation
    session.commit()
    repository.record_candidate(session, generation, 'b' * 64)
    session.commit()
    repository.switch_candidate(session, generation)
    assert runtime.container_id == 'b' * 64
    session.rollback()
    assert runtime.container_id == 'a' * 64
    assert operation.state == 'preparing' and operation.candidate_container_id == 'b' * 64
    assert session.query(RuntimeContainerDeletion).count() == 0
    repository.switch_candidate(session, generation)
    session.commit()
    assert runtime.container_id == 'b' * 64 and operation.state == 'switched'
    assert session.query(RuntimeContainerDeletion).one().container_id == 'a' * 64
    assert repository.switch_candidate(session, generation).id == runtime.id


def test_admission_replays_one_operation_and_fresh_role_denies_switch(
    session: Session, test_workspace: Workspace, test_user: User
) -> None:
    operation = repository.admit(session, test_workspace.id, test_user.id)
    session.commit()
    assert repository.admit(session, test_workspace.id, test_user.id).generation == operation.generation
    repository.record_candidate(session, operation.generation, 'b' * 64)
    session.commit()
    member = session.query(WorkspaceMember).one()
    session.execute(update(WorkspaceMember).values(role='viewer'), execution_options={'synchronize_session': False})
    session.commit()
    with pytest.raises(PermissionDeniedError):
        repository.switch_candidate(session, operation.generation)
    assert member.role == 'viewer'


def test_cancelled_operation_cannot_publish_or_accept_late_candidate(
    session: Session, test_workspace: Workspace, test_user: User
) -> None:
    operation = repository.admit(session, test_workspace.id, test_user.id)
    session.commit()
    repository.cancel_runtime(session, operation.runtime_id)
    session.commit()
    with pytest.raises(ConflictError):
        repository.record_candidate(session, operation.generation, 'b' * 64)
    with pytest.raises(ConflictError):
        repository.switch_candidate(session, operation.generation)
    assert session.query(OpenCodeRuntime).one().container_id is None


def test_retirement_failure_keeps_identity_and_replays_after_owner_deleted(
    session: Session, test_workspace: Workspace, test_user: User
) -> None:
    operation = repository.admit(session, test_workspace.id, test_user.id)
    session.commit()
    repository.cancel_runtime(session, operation.runtime_id)
    session.query(OpenCodeRuntime).delete()
    session.commit()
    factory = lambda: Session(bind=session.get_bind(), join_transaction_mode='create_savepoint')

    def reject(*args: object) -> None:
        raise RuntimeError('private detail')

    assert retire_cancelled_operations(session_factory=factory, controller_factory=cast(Any, lambda: SimpleNamespace(retire_generation=reject))) == 0
    session.expire_all()
    assert operation.finished_at is None and operation.last_error_type == 'RuntimeError' and operation.attempts == 1
    operation.next_attempt_at = utc_now()
    session.commit()
    calls: list[tuple[object, ...]] = []

    def record_call(*args: object) -> None:
        calls.append(args)

    assert retire_cancelled_operations(session_factory=factory, controller_factory=cast(Any, lambda: SimpleNamespace(retire_generation=record_call))) == 1
    session.expire_all()
    assert calls == [(test_workspace.id, test_user.id, operation.runtime_id, operation.generation)]
    assert operation.finished_at is not None and operation.last_error_type is None


@pytest.fixture
def provisioner(session: Session, test_workspace: Workspace, test_user: User, monkeypatch: pytest.MonkeyPatch) -> Any:
    from app.features.workspace import docker_service
    from runtime_controller.policy import POLICY_VERSION
    containers: dict[str, Any] = {}
    calls: list[str] = []

    class Container:
        def __init__(self, identity: str) -> None:
            self.id = identity
            self.status = 'created'
            self.config: dict[str, Any] = {}

        def payload(self) -> dict[str, Any]:
            return {
                "id": self.id,
                "name": "skycloud-runtime-" + self.id,
                "status": self.status,
                "image": docker_service.OPENCODE_IMAGE,
                "labels": {"skycloud.runtime_policy": POLICY_VERSION},
                "ports": {},
            }

        def start(self) -> None:
            self.status = 'running'

    class Controller:
        def prepare(self, workspace_id: int, user_id: int, runtime_id: int, generation: str) -> dict[str, Any]:
            calls.append(generation)
            containers.setdefault('b' * 64, Container('b' * 64))
            return containers['b' * 64].payload()

        def inspect(self, identity: str) -> dict[str, Any]:
            return containers[identity].payload()

        def action(self, identity: str, action: str) -> None:
            if action in {"start", "restart"}:
                containers[identity].start()
            elif action == "stop":
                containers[identity].status = "stopped"

        def read_config(self, identity: str) -> dict[str, Any]:
            return containers[identity].config.copy()

        def write_config(self, identity: str, config: dict[str, Any]) -> None:
            containers[identity].config = config.copy()

    def noop_sync(*args: object, **kwargs: object) -> None:
        pass

    monkeypatch.setattr(docker_service, '_client', Controller)
    monkeypatch.setattr(docker_service, '_sync_mcp_via_runtime_api', noop_sync)
    return containers, calls, Container


def test_production_start_prepares_publishes_configures_and_completes(
    session: Session, test_workspace: Workspace, test_user: User, provisioner: Any
) -> None:
    from app.features.workspace import docker_service
    from app.models.mcp_runtime_token import McpRuntimeToken
    containers, calls, _ = provisioner
    docker_service.start(session, test_workspace, test_user.id)
    operation = session.query(RuntimeOperation).one()
    runtime = session.query(OpenCodeRuntime).one()
    assert calls == [operation.generation]
    assert operation.state == 'complete' and operation.finished_at is not None
    assert runtime.container_id == 'b' * 64 and runtime.status == 'running' and runtime.config_version == 1
    assert len(session.query(McpRuntimeToken).filter_by(revoked_at=None).all()) == 2
    assert 'SKYCLOUD' in containers['b' * 64].config['mcp']


def test_worker_recovers_committed_admission_with_same_generation(
    session: Session, test_workspace: Workspace, test_user: User, provisioner: Any
) -> None:
    from app.infra.runtime_operations import recover_runtime_operations
    operation = repository.admit(session, test_workspace.id, test_user.id)
    generation = operation.generation
    session.commit()  # Simulate death before prepare.
    factory = lambda: Session(bind=session.get_bind(), join_transaction_mode='create_savepoint')
    assert recover_runtime_operations(session_factory=factory) == 1
    session.expire_all()
    assert operation.state == 'complete' and provisioner[1] == [generation]


def test_configuration_rechecks_cancel_after_token_commit(
    session: Session, test_workspace: Workspace, test_user: User, provisioner: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.features.workspace import docker_service
    from app.features.runtime_llm import tokens
    issue = tokens.issue_token

    def cancel_after_issue(db: Session, runtime: OpenCodeRuntime) -> str:
        token = issue(db, runtime)
        repository.cancel_runtime(db, runtime.id)
        db.commit()
        return token

    monkeypatch.setattr(tokens, 'issue_token', cancel_after_issue)
    with pytest.raises(ConflictError):
        docker_service.start(session, test_workspace, test_user.id)
    assert session.query(RuntimeOperation).one().state == 'cancelled'
    assert provisioner[0]['b' * 64].config == {}


def test_stale_worker_generation_cannot_start_completed_or_new_operation(
    session: Session, test_workspace: Workspace, test_user: User, provisioner: Any
) -> None:
    from app.features.workspace import docker_service
    docker_service.start(session, test_workspace, test_user.id, expected_generation='f' * 32)
    assert provisioner[1] == [] and session.query(RuntimeOperation).count() == 0


def test_cancelled_candidate_cannot_receive_configuration_as_current_runtime(
    session: Session, test_workspace: Workspace, test_user: User, provisioner: Any
) -> None:
    from app.features.workspace import docker_service
    docker_service.start(session, test_workspace, test_user.id)
    operation = session.query(RuntimeOperation).one()
    runtime = session.query(OpenCodeRuntime).one()
    operation.state = 'cancelled'
    session.commit()
    before = dict(provisioner[0]['b' * 64].config)
    with pytest.raises(ConflictError):
        docker_service.setup_mcp(session, test_workspace, test_user.id, runtime=runtime)
    assert provisioner[0]['b' * 64].config == before


def test_summary_observation_cannot_cancel_pending_start(
    session: Session, test_workspace: Workspace, test_user: User, provisioner: Any
) -> None:
    from app.features.workspace import docker_service
    operation = repository.admit(session, test_workspace.id, test_user.id)
    session.commit()
    repository.record_candidate(session, operation.generation, 'b' * 64)
    session.commit()
    runtime = repository.switch_candidate(session, operation.generation)
    session.commit()
    docker_service._sync_runtime_status(session, runtime)  # pyright: ignore[reportPrivateUsage]
    assert runtime.status == 'provisioning'

    # 批量快照表明候选容器已停止. 条件 SQL 更新
    # 绝不能篡改已准入操作的标识/状态.
    class Client:
        def user_snapshot(self, user: int) -> list[Any]:
            return []

    from unittest.mock import patch
    with patch.object(docker_service, '_client', Client):
        docker_service.list_summaries(session, cast(Any, [({'id': test_workspace.id}, runtime)]), test_user.id)
    session.expire_all()
    assert runtime.status == 'provisioning' and runtime.container_id == 'b' * 64
    assert operation.state == 'switched'


def test_missing_candidate_replacement_cancels_old_and_preserves_previous_container(
    session: Session, test_workspace: Workspace, test_user: User
) -> None:
    runtime = OpenCodeRuntime(workspace_id=test_workspace.id, user_id=test_user.id, container_id='a' * 64, status='running')
    session.add(runtime)
    session.commit()
    old = repository.admit(session, test_workspace.id, test_user.id)
    session.commit()
    repository.record_candidate(session, old.generation, 'b' * 64)
    session.commit()
    replacement = repository.replace_missing(session, old.generation, 'b' * 64)
    session.commit()
    assert old.state == 'cancelled' and replacement.generation != old.generation
    assert replacement.previous_container_id == 'a' * 64 and runtime.container_id == 'a' * 64
    assert session.query(RuntimeContainerDeletion).count() == 0


def test_missing_switched_candidate_replacement_rollback_is_atomic(
    session: Session, test_workspace: Workspace, test_user: User
) -> None:
    old = repository.admit(session, test_workspace.id, test_user.id)
    session.commit()
    repository.record_candidate(session, old.generation, 'b' * 64)
    session.commit()
    runtime = repository.switch_candidate(session, old.generation)
    session.commit()
    replacement = repository.replace_missing(session, old.generation, 'b' * 64)
    generation = replacement.generation
    assert runtime.container_id is None and old.state == 'cancelled'
    session.rollback()
    assert runtime.container_id == 'b' * 64 and old.state == 'switched'
    assert session.get(RuntimeOperation, generation) is None
    assert session.query(RuntimeContainerDeletion).count() == 0


def test_missing_candidate_cannot_replace_another_identity(
    session: Session, test_workspace: Workspace, test_user: User
) -> None:
    old = repository.admit(session, test_workspace.id, test_user.id)
    session.commit()
    repository.record_candidate(session, old.generation, 'b' * 64)
    session.commit()
    with pytest.raises(ConflictError):
        repository.replace_missing(session, old.generation, 'c' * 64)
    assert old.state == 'preparing' and session.query(RuntimeOperation).count() == 1


def test_stop_commits_cancellation_before_rpc_so_crash_cannot_resume(
    session: Session, test_workspace: Workspace, test_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """停止在 RPC 之后丢失不得使代系处于可恢复状态."""
    from app.features.workspace import docker_service
    from app.infra.runtime_operations import recover_runtime_operations
    events: list[tuple[str, Any]] = []

    class Controller:
        def action(self, identity: str, action: str) -> None:
            events.append((action, identity))

        def retire_generation(self, *args: object) -> None:
            events.append(('retire', args))

    monkeypatch.setattr(docker_service, '_client', Controller)

    runtime = OpenCodeRuntime(workspace_id=test_workspace.id, user_id=test_user.id, container_id='a' * 64, status='running')
    session.add(runtime)
    session.flush()
    operation = repository.admit(session, test_workspace.id, test_user.id)
    session.commit()
    generation = operation.generation

    def crash(*args: object, **kwargs: object) -> None:
        raise RuntimeError('process lost after the stop RPC')

    monkeypatch.setattr(docker_service.runtime_token_service, 'revoke_runtime_tokens', crash)
    with pytest.raises(RuntimeError):
        docker_service.stop(session, test_workspace, test_user.id)
    assert ('stop', 'a' * 64) in events
    session.rollback()

    factory = lambda: Session(bind=session.get_bind(), join_transaction_mode='create_savepoint')
    with factory() as observer:
        obs_op = observer.get(RuntimeOperation, generation)
        assert obs_op is not None and obs_op.state == 'cancelled'
    assert recover_runtime_operations(session_factory=factory) == 0
    with factory() as observer:
        obs_op = observer.get(RuntimeOperation, generation)
        assert obs_op is not None and obs_op.state == 'cancelled'
    assert not [event for event in events if event[0] == 'start']


def test_stop_keeps_the_container_and_stages_no_deletion(
    session: Session, test_workspace: Workspace, test_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """正常停止绝不得被视为物理删除的触发条件."""
    from app.features.workspace import docker_service
    events: list[tuple[str, str]] = []

    class Controller:
        def action(self, identity: str, action: str) -> None:
            events.append((action, identity))

    monkeypatch.setattr(docker_service, '_client', Controller)

    runtime = OpenCodeRuntime(workspace_id=test_workspace.id, user_id=test_user.id, container_id='a' * 64, status='running')
    session.add(runtime)
    session.commit()

    docker_service.stop(session, test_workspace, test_user.id)
    session.expire_all()
    stopped = session.get(OpenCodeRuntime, runtime.id)
    assert stopped is not None
    assert stopped.status == 'stopped' and stopped.container_id == 'a' * 64
    assert events == [('stop', 'a' * 64)]
    assert session.query(RuntimeContainerDeletion).count() == 0


def test_start_conflict_after_prepare_retires_candidate_instead_of_leaving_it(
    session: Session, test_workspace: Workspace, test_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """在准备中途取消的代系必须由调用方退役, 不能遗留游离代系."""
    from app.features.workspace import docker_service
    events: list[tuple[str, Any]] = []
    prepared = {"id": 'b' * 64}

    class Controller:
        def prepare(self, workspace_id: int, user_id: int, runtime_id: int, generation: str) -> dict[str, Any]:
            events.append(('prepare', generation))
            # 在准备阶段进行期间, 另一行为体取消了已准入的操作.
            with Session(bind=session.get_bind(), join_transaction_mode='create_savepoint') as other:
                other.query(RuntimeOperation).filter(
                    RuntimeOperation.runtime_id == runtime_id,
                    RuntimeOperation.state == 'preparing'
                ).one().state = 'cancelled'
                other.commit()
            return prepared

        def retire_generation(self, *args: object) -> None:
            events.append(('retire', args))

    def noop_sync(*args: object, **kwargs: object) -> None:
        pass

    monkeypatch.setattr(docker_service, '_client', Controller)
    monkeypatch.setattr(docker_service, '_sync_mcp_via_runtime_api', noop_sync)

    with pytest.raises(ConflictError):
        docker_service.start(session, test_workspace, test_user.id)
    assert [event[0] for event in events] == ['prepare', 'retire']
    assert events[1][1][3] == events[0][1]
    assert events[1][1][:1] == (test_workspace.id,)


def test_start_publication_conflict_survives_retirement_failure(
    session: Session, test_workspace: Workspace, test_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """失败的即时补偿不能取代发布冲突错误."""
    from app.features.workspace import docker_service

    candidates: dict[tuple[int, int, int, str], dict[str, str]] = {}
    retirements: list[tuple[object, ...]] = []

    class Controller:
        def prepare(self, workspace_id: int, user_id: int, runtime_id: int, generation: str) -> dict[str, str]:
            identity = (workspace_id, user_id, runtime_id, generation)
            candidates[identity] = {"id": 'b' * 64}
            with Session(
                bind=session.get_bind(), join_transaction_mode='create_savepoint'
            ) as other:
                repository.cancel_runtime(other, runtime_id)
                other.commit()
            return candidates[identity]

        def retire_generation(self, *identity: object) -> None:
            retirements.append(identity)
            raise RuntimeError('private controller retirement failure')

    monkeypatch.setattr(docker_service, '_client', Controller)

    with pytest.raises(ConflictError, match='Runtime operation is no longer active'):
        docker_service.start(session, test_workspace, test_user.id)
    assert len(candidates) == 1
    assert retirements == list(candidates)


def test_recovery_defers_failed_generation_and_completes_later_generation(
    session: Session, test_workspace: Workspace, test_user: User, admin_user: User, provisioner: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """非预期的 Controller 失败不能搁浅另一已准入的所有者."""
    from datetime import timedelta

    from app.features.workspace import docker_service
    from app.infra.runtime_operations import recover_runtime_operations

    session.add(WorkspaceMember(
        workspace_id=test_workspace.id, user_id=admin_user.id, role='admin'
    ))
    session.flush()
    first = repository.admit(session, test_workspace.id, test_user.id)
    second = repository.admit(session, test_workspace.id, admin_user.id)
    first_generation, second_generation = first.generation, second.generation
    # 排序是显式的, 而非依赖 UUID 或时钟精度.
    first.next_attempt_at = utc_now() - timedelta(seconds=2)
    second.next_attempt_at = utc_now() - timedelta(seconds=1)
    session.commit()
    controller_cls: Any = docker_service._client  # pyright: ignore[reportPrivateUsage]
    unavailable = True
    containers, _, container_type = provisioner

    class Controller(controller_cls):
        def prepare(self, workspace_id: int, user_id: int, runtime_id: int, generation: str) -> dict[str, Any]:
            if generation == first_generation and unavailable:
                raise RuntimeError('private controller recovery failure')
            container: dict[str, Any] = cast(
                dict[str, Any], super().prepare(workspace_id, user_id, runtime_id, generation)  # pyright: ignore[reportUnknownMemberType]
            )
            if generation == first_generation:
                replacement: Any = containers.setdefault('c' * 64, container_type('c' * 64))
                return cast(dict[str, Any], replacement.payload())
            return container

    monkeypatch.setattr(docker_service, '_client', Controller)
    factory = lambda: Session(
        bind=session.get_bind(), join_transaction_mode='create_savepoint'
    )
    before = utc_now()
    assert recover_runtime_operations(session_factory=factory) == 1
    after = utc_now()

    with factory() as observer:
        failed = observer.get(RuntimeOperation, first_generation)
        assert failed is not None
        assert failed.state == 'preparing'
        assert failed.finished_at is None and failed.candidate_container_id is None
        assert failed.attempts == 1 and failed.last_error_type == 'RuntimeError'
        assert failed.next_attempt_at is not None
        assert (
            before + timedelta(seconds=2)
            <= failed.next_attempt_at
            <= after + timedelta(seconds=2)
        )
        completed = observer.get(RuntimeOperation, second_generation)
        assert completed is not None
        assert completed.state == 'complete' and completed.finished_at is not None
        rt_comp = observer.get(OpenCodeRuntime, completed.runtime_id)
        assert rt_comp is not None
        assert rt_comp.status == 'running'
        failed.next_attempt_at = before - timedelta(seconds=1)
        observer.commit()

    unavailable = False
    assert recover_runtime_operations(session_factory=factory) == 1
    with factory() as observer:
        retried = observer.get(RuntimeOperation, first_generation)
        assert retried is not None
        assert retried.state == 'complete' and retried.finished_at is not None
        assert retried.last_error_type is None
        rt_retried = observer.get(OpenCodeRuntime, retried.runtime_id)
        assert rt_retried is not None
        assert rt_retried.container_id == 'c' * 64
        assert observer.query(RuntimeOperation).count() == 2
