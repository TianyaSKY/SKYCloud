"""Lifecycle state and retry behavior remain intact with private failure diagnostics."""
from unittest.mock import MagicMock

import pytest
from loguru import logger
from pika.exceptions import AMQPError

from app.features.workspace import docker_service, service
from app.infra import task_queue
from app.models.opencode_runtime import OpenCodeRuntime
from app.models.mcp_runtime_token import McpRuntimeToken
from app.models.user import User
from app.models.workspace import WorkspaceMember

SECRET = 'private-runtime-config-and-queue-sentinel'


@pytest.fixture
def diagnostics():
    messages = []
    sink = logger.add(lambda message: messages.append((str(message), message.record['exception'])), format='{message}')
    yield messages
    logger.remove(sink)


def safe(messages):
    assert any('error_type=' in message for message, _ in messages)
    assert all(SECRET not in message and exception is None for message, exception in messages)


@pytest.fixture
def runtime(session, test_workspace, test_user):
    row = OpenCodeRuntime(workspace_id=test_workspace.id, user_id=test_user.id, container_id='a'*64, status='running')
    session.add(row); session.commit()
    return row


@pytest.mark.parametrize('action', ['start', 'stop', 'restart', 'legacy-stop', 'setup'])
def test_lifecycle_rpc_failure_keeps_safe_state(session, test_workspace, test_user, runtime, monkeypatch, diagnostics, action):
    error = docker_service.DockerException(SECRET)
    client = MagicMock()
    client.containers.get.side_effect = error
    monkeypatch.setattr(docker_service, '_client', lambda: client)
    if action == 'legacy-stop':
        test_workspace.container_id = runtime.container_id
        session.commit()
        assert docker_service.stop(session, test_workspace) is test_workspace
        assert test_workspace.status == 'error' and SECRET not in test_workspace.error_message
    elif action == 'setup':
        with pytest.raises(docker_service.DockerException) as raised:
            docker_service.setup_mcp(session, test_workspace, test_user.id, runtime=runtime)
        assert raised.value is error
        tokens = session.query(McpRuntimeToken).filter_by(runtime_id=runtime.id).all()
        assert tokens and all(token.revoked_at is not None for token in tokens)
    else:
        assert getattr(docker_service, action)(session, test_workspace, test_user.id) is test_workspace
        session.expire_all()
        assert runtime.status == 'error' and SECRET not in runtime.error_message
        assert runtime.container_id == 'a'*64
    safe(diagnostics)


@pytest.mark.parametrize('operation', ['legacy-status', 'runtime-status', 'list', 'image', 'access'])
def test_snapshot_failure_does_not_mutate_identity(session, test_workspace, test_user, runtime, monkeypatch, diagnostics, operation):
    client = MagicMock()
    client.containers.get.side_effect = docker_service.DockerException(SECRET)
    client.user_snapshot.side_effect = docker_service.DockerException(SECRET)
    monkeypatch.setattr(docker_service, '_client', lambda: client)
    if operation == 'legacy-status':
        test_workspace.container_id = 'a'*64
        test_workspace.status = 'running'
        session.commit()
        docker_service._sync_status(session, test_workspace)
        assert test_workspace.status == 'running'
    elif operation == 'runtime-status':
        docker_service._sync_runtime_status(session, runtime)
    elif operation == 'list':
        assert len(docker_service.list_summaries(session, [(test_workspace.to_dict(), runtime)], test_user.id)) == 1
    elif operation == 'image':
        assert docker_service.runtime_uses_configured_image(runtime) is True
    else:
        assert docker_service.get_access_url(runtime) is None
    assert runtime.container_id == 'a'*64 and runtime.status == 'running'
    safe(diagnostics)


def test_role_refresh_failure_keeps_committed_role_and_revocation(session, test_workspace, test_user, monkeypatch, diagnostics):
    member_user = User(username='diagnostic-member', password_hash='test-only', role='common')
    session.add(member_user); session.flush()
    member = WorkspaceMember(workspace_id=test_workspace.id, user_id=member_user.id, role='editor')
    runtime = OpenCodeRuntime(workspace_id=test_workspace.id, user_id=member_user.id, container_id='b'*64, status='running')
    session.add_all([member, runtime]); session.commit()
    token, _ = docker_service.runtime_token_service.refresh_runtime_token(session, runtime, role='editor')
    monkeypatch.setattr(docker_service, 'setup_mcp', MagicMock(side_effect=RuntimeError(SECRET)))
    assert service.update_member_role(session, test_workspace.id, test_user.id, member_user.id, 'viewer').role == 'viewer'
    session.expire_all()
    assert member.role == 'viewer' and token.revoked_at is not None
    safe(diagnostics)


def test_polling_recovers_even_when_failed_connection_close_also_fails(monkeypatch, diagnostics):
    consumer = task_queue.RabbitMQTaskConsumer()
    connection = MagicMock()
    connection.is_open = True
    connection.close.side_effect = AMQPError(SECRET)
    consumer._connection = connection
    consumer._channel = MagicMock()
    message = task_queue.QueueMessage(task_queue.FILE_PROCESS_QUEUE, 'preserved', 1, 2)
    consumer.get_message = MagicMock(side_effect=[AMQPError(SECRET), message])
    sleep = MagicMock()
    monkeypatch.setattr(task_queue.time, 'sleep', sleep)
    assert consumer.get_next_message() is message
    assert consumer._connection is None and consumer._channel is None
    sleep.assert_called_once_with(task_queue.RABBITMQ_RECONNECT_DELAY_SECONDS)
    safe(diagnostics)
