"""Exercise the real list route, SQL counts, runtime ownership and reconciliation."""
from datetime import timedelta
from unittest.mock import Mock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event

from app.features.auth.service import generate_token
from app.features.workspace.router import router
from app.features.workspace.service import create_workspace
from app.infra.datetime_utils import beijing_now
from app.infra.extensions import get_db, get_session_factory
from sqlalchemy.orm import sessionmaker
from app.models.opencode_runtime import OpenCodeRuntime
from app.models.mcp_runtime_token import McpRuntimeToken
from runtime_controller.client import ControllerError


@pytest.mark.parametrize('count', [1, 25])
@pytest.mark.parametrize('state', ['running', 'missing', 'outage'])
def test_http_list_has_constant_sql_and_controller_calls(session, test_user, admin_user, count, state):
    user_id = test_user.id
    auth = {'Authorization': 'Bearer ' + generate_token(user_id, test_user.auth_version)}
    expected, snapshot = {}, []
    for number in range(count):
        workspace = create_workspace(session, user_id, f'Runtime {number}')
        runtime = OpenCodeRuntime(workspace_id=workspace.id, user_id=user_id,
                                  container_id=f'{number + 1:064x}', status='stopped', config_version=2)
        foreign = OpenCodeRuntime(workspace_id=workspace.id, user_id=admin_user.id,
                                  container_id='f' * 64, status='running', config_version=99)
        session.add_all([runtime, foreign]); session.flush()
        session.add(McpRuntimeToken(runtime_id=runtime.id, user_id=user_id,
            workspace_id=workspace.id, jti=f'list-{number}', token_hash=f'{number + 1:064x}',
            token_preview='hash-only', expires_at=beijing_now() + timedelta(hours=1)))
        expected[workspace.id] = (runtime.id, runtime.container_id)
        snapshot.append(dict(id=runtime.container_id, runtime_id=runtime.id,
                             workspace_id=workspace.id, user_id=user_id, status='running'))
    session.commit()
    # Expire identities to expose accidental lazy loading during commit/serialization.
    session.expire_all()
    app = FastAPI(); app.include_router(router, prefix='/api')
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_session_factory] = lambda: sessionmaker(bind=session.get_bind(), join_transaction_mode="create_savepoint")
    controller = Mock()
    if state == 'outage':
        controller.user_snapshot.side_effect = ControllerError('offline')
    else:
        controller.user_snapshot.return_value = snapshot if state == 'running' else []
    statements = []
    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    engine = session.get_bind()
    with TestClient(app) as client, patch('app.features.workspace.docker_service._client', return_value=controller):
        event.listen(engine, 'before_cursor_execute', record)
        try:
            response = client.get('/api/workspace', headers=auth)
        finally:
            event.remove(engine, 'before_cursor_execute', record)
    assert response.status_code == 200, response.text
    controller.user_snapshot.assert_called_once_with(user_id)
    controller.containers.get.assert_not_called()
    selects = [sql for sql in statements if sql.lstrip().upper().startswith('SELECT')]
    assert len([sql for sql in selects if 'FROM workspaces ' in sql]) == 1
    assert len(selects) <= 4, statements  # Includes actual authentication reads.
    data_statements = [sql for sql in statements if sql.lstrip().split()[0].upper() in {'SELECT', 'UPDATE', 'INSERT', 'DELETE'}]
    assert sum(sql.lstrip().startswith("SELECT users.role") for sql in selects) == 1
    assert len(data_statements) <= 6, statements  # Three auth reads + joined list + two bulk updates.
    rows = {row['id']: row for row in response.json()['data']['workspaces']}
    for workspace_id, (runtime_id, container_id) in expected.items():
        row = rows[workspace_id]
        assert row['runtime_id'] == runtime_id and row['config_version'] == 2
        assert row['status'] == ('running' if state == 'running' else 'stopped')
        assert row['container_id'] == (None if state == 'missing' else container_id[:12])
        assert row['access_url'] is None
    session.expire_all()
    tokens = session.query(McpRuntimeToken).filter(McpRuntimeToken.jti.like('list-%')).all()
    assert all((token.revoked_at is not None) == (state == 'missing') for token in tokens)
    own = session.query(OpenCodeRuntime).filter_by(user_id=user_id).all()
    assert all(runtime.status == ('running' if state == 'running' else 'stopped') for runtime in own)
    others = session.query(OpenCodeRuntime).filter_by(user_id=admin_user.id).all()
    assert all(runtime.status == 'running' and runtime.container_id == 'f' * 64 for runtime in others)


def test_old_list_snapshot_does_not_overwrite_recreated_runtime(session, test_user):
    from sqlalchemy import update
    from app.features.workspace.service import get_user_workspace_rows
    from app.features.workspace.docker_service import list_summaries
    workspace = create_workspace(session, test_user.id, 'Recreation race')
    runtime = OpenCodeRuntime(workspace_id=workspace.id, user_id=test_user.id,
                              container_id='a' * 64, status='running')
    session.add(runtime); session.flush()
    token = McpRuntimeToken(runtime_id=runtime.id, user_id=test_user.id,
        workspace_id=workspace.id, jti='recreated', token_hash='e' * 64,
        token_preview='hash-only', expires_at=beijing_now() + timedelta(hours=1))
    session.add(token); session.commit()
    rows = get_user_workspace_rows(session, test_user.id)
    runtime_id, user_id = runtime.id, test_user.id
    def snapshot(_):
        # Model a concurrent committed recreation between observation and write.
        # Leave this Session's identity map stale, as a second Session would.
        session.execute(update(OpenCodeRuntime).where(OpenCodeRuntime.id == runtime_id)
            .values(container_id='b' * 64, status='running', config_version=5),
            execution_options={'synchronize_session': False})
        return []
    controller = Mock(); controller.user_snapshot.side_effect = snapshot
    with patch('app.features.workspace.docker_service._client', return_value=controller):
        list_summaries(session, rows, user_id)
    session.expire_all()
    current = session.get(OpenCodeRuntime, runtime_id)
    assert current.container_id == 'b' * 64 and current.status == 'running'
    assert current.config_version == 5
    assert session.query(McpRuntimeToken).filter_by(jti='recreated').one().revoked_at is None
