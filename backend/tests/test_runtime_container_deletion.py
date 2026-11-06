"""使用真实 SQLite 的事务性回退与清理身份契约测试."""
from typing import Any
import pytest
from sqlalchemy.orm import Session
from app.features.workspace.docker_service import remove_runtime
from app.infra.datetime_utils import utc_now
from app.infra.runtime_container_deletion import delete_pending_containers
from app.models.opencode_runtime import OpenCodeRuntime
from app.models.runtime_container_deletion import RuntimeContainerDeletion
from app.models.mcp_runtime_token import McpRuntimeToken
from app.models.user import User
from app.models.workspace import Workspace
from runtime_controller.client import ControllerNotFound


def reaper(session: Session, factory: Any) -> int:
    return delete_pending_containers(
        session_factory=lambda: Session(bind=session.get_bind(), join_transaction_mode='create_savepoint'),
        controller_factory=factory,
    )


def seed_runtime(session: Session, workspace: Workspace, user: User) -> tuple[OpenCodeRuntime, McpRuntimeToken]:
    runtime = OpenCodeRuntime(workspace_id=workspace.id, user_id=user.id, container_id='a' * 64, status='running')
    session.add(runtime)
    session.flush()
    token = McpRuntimeToken(
        runtime_id=runtime.id,
        workspace_id=workspace.id,
        user_id=user.id,
        jti='cleanup-jti',
        token_hash='cleanup-hash',
        token_preview='p',
        expires_at=utc_now(),
    )
    session.add(token)
    session.commit()
    return runtime, token


def test_detach_and_credential_revocation_rollback_together(
    session: Session, test_workspace: Workspace, test_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.features.workspace import docker_service

    def mock_client() -> Any:
        raise AssertionError('RPC before commit')

    monkeypatch.setattr(docker_service, '_client', mock_client)
    runtime, token = seed_runtime(session, test_workspace, test_user)
    remove_runtime(runtime, session)
    assert runtime.container_id is None and token.revoked_at is not None
    assert session.query(RuntimeContainerDeletion).count() == 1
    session.rollback()
    assert runtime.container_id == 'a' * 64 and token.revoked_at is None
    assert session.query(RuntimeContainerDeletion).count() == 0


def test_controller_failure_persists_type_and_missing_replay_completes(session: Session) -> None:
    session.add(RuntimeContainerDeletion(container_id='a' * 64))
    session.commit()

    class RejectingController:
        def inspect(self, _ref: str) -> Any:
            raise RuntimeError('sensitive details must not be retained')

    assert reaper(session, RejectingController) == 0
    session.expire_all()
    row = session.query(RuntimeContainerDeletion).one()
    assert row.attempts == 1 and row.last_error_type == 'RuntimeError' and row.completed_at is None
    row.next_attempt_at = utc_now()
    session.commit()

    class MissingController:
        def inspect(self, _ref: str) -> Any:
            raise ControllerNotFound('already gone')

    assert reaper(session, MissingController) == 1
    session.expire_all()
    assert row.completed_at is not None and row.last_error_type is None and row.attempts == 2


def test_alias_reference_cannot_remove_a_different_full_identity(session: Session) -> None:
    removed: list[bool] = []
    session.add(RuntimeContainerDeletion(container_id='a' * 12))
    session.commit()
    container: dict[str, Any] = {
        "id": 'a' * 64,
        "name": "runtime",
        "status": "running",
        "image": None,
        "labels": {},
        "ports": {},
    }

    class Controller:
        def inspect(self, _ref: str) -> dict[str, Any]:
            return container

        def action(self, _ref: str, _action: str) -> None:
            removed.append(True)

    assert reaper(session, Controller) == 0
    assert removed == []
    session.expire_all()
    row = session.query(RuntimeContainerDeletion).one()
    assert row.completed_at is None and row.last_error_type == 'ControllerError'


def test_live_reference_is_retained_then_detach_stages_new_cleanup(
    session: Session, test_workspace: Workspace, test_user: User
) -> None:
    runtime, _ = seed_runtime(session, test_workspace, test_user)
    assert runtime.container_id is not None
    session.add(RuntimeContainerDeletion(container_id=runtime.container_id))
    session.commit()
    removed: list[bool] = []
    container: dict[str, Any] = {
        "id": 'a' * 64,
        "name": "runtime",
        "status": "running",
        "image": None,
        "labels": {},
        "ports": {},
    }

    class Controller:
        def inspect(self, _ref: str) -> dict[str, Any]:
            return container

        def action(self, _ref: str, action: str) -> None:
            assert action == "remove"
            removed.append(True)

    factory = Controller
    assert reaper(session, factory) == 1 and removed == []
    remove_runtime(runtime, session)
    session.commit()
    assert reaper(session, factory) == 1 and removed == [True]


def test_old_controller_missing_legacy_route_keeps_cleanup_pending(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx
    from runtime_controller.client import ControllerClient
    monkeypatch.setenv('RUNTIME_CONTROLLER_TOKEN', 'T' * 48)

    def response(method: str, url: str, **kwargs: object) -> httpx.Response:
        if '/retired-containers/' in url:
            return httpx.Response(404, json={'detail': 'Not Found'})
        return httpx.Response(403, json={'detail': 'Historical identity rejected'})

    monkeypatch.setattr(httpx, 'request', response)
    session.add(RuntimeContainerDeletion(container_id='a' * 64))
    session.commit()
    assert reaper(session, ControllerClient) == 0
    session.expire_all()
    row = session.query(RuntimeContainerDeletion).one()
    assert row.completed_at is None and row.attempts == 1 and row.last_error_type == 'ControllerError'


def test_missing_full_id_still_retires_known_generation_before_completion(
    session: Session, test_workspace: Workspace, test_user: User
) -> None:
    from app.models.runtime_operation import RuntimeOperation
    runtime, _ = seed_runtime(session, test_workspace, test_user)
    generation = 'b' * 32
    session.add(RuntimeOperation(
        generation=generation,
        workspace_id=test_workspace.id,
        user_id=test_user.id,
        runtime_id=runtime.id,
        candidate_container_id=runtime.container_id,
        state='complete',
        finished_at=utc_now(),
    ))
    session.commit()
    remove_runtime(runtime, session)
    session.commit()
    calls: list[tuple[object, ...]] = []

    class MissingGenerationController:
        def inspect(self, _ref: str) -> Any:
            raise ControllerNotFound('old ID absent')

        def retire_generation(self, *args: object) -> None:
            calls.append(args)

    assert reaper(session, MissingGenerationController) == 1
    assert calls == [(test_workspace.id, test_user.id, runtime.id, generation)]


def test_missing_id_generation_retirement_failure_keeps_intent(
    session: Session, test_workspace: Workspace, test_user: User
) -> None:
    from app.models.runtime_operation import RuntimeOperation
    from runtime_controller.client import ControllerError
    runtime, _ = seed_runtime(session, test_workspace, test_user)
    session.add(RuntimeOperation(
        generation='b' * 32,
        workspace_id=test_workspace.id,
        user_id=test_user.id,
        runtime_id=runtime.id,
        candidate_container_id=runtime.container_id,
        state='complete',
        finished_at=utc_now(),
    ))
    session.commit()
    remove_runtime(runtime, session)
    session.commit()

    class FailedRetirementController:
        def inspect(self, _ref: str) -> Any:
            raise ControllerNotFound('old ID absent')

        def retire_generation(self, *_args: object) -> None:
            raise ControllerError('unsupported retirement')

    assert reaper(session, FailedRetirementController) == 0
    session.expire_all()
    intent = session.query(RuntimeContainerDeletion).one()
    assert intent.completed_at is None and intent.last_error_type == 'ControllerError'
