"""Real PostgreSQL list reconciliation and concurrent recreation checks."""
import os
from datetime import timedelta
from unittest.mock import Mock, patch

import pytest
from sqlalchemy import event, update
from sqlalchemy.orm import Session

from entry.migrate import upgrade
from app.features.workspace.service import create_workspace, get_user_workspace_rows
from app.features.workspace.docker_service import list_summaries
from app.infra.datetime_utils import beijing_now
from app.models.user import User
from app.models.opencode_runtime import OpenCodeRuntime
from app.models.mcp_runtime_token import McpRuntimeToken
from tests.integration.test_migrations import postgres

pytestmark = pytest.mark.skipif(os.getenv('RUN_WORKSPACE_LIST_TESTS') != '1',
                               reason='Opt-in isolated PostgreSQL workspace list checks')


@pytest.mark.parametrize('state', ['running', 'missing', 'recreated'])
def test_postgres_bulk_list_and_concurrent_recreation(postgres, state):
    upgrade(postgres)
    with Session(postgres) as session:
        user = User(username='list-owner', password_hash='isolated-test-hash', role='common')
        session.add(user); session.commit()
        user_id = user.id
        expected, snapshot = {}, []
        for number in range(25):
            workspace = create_workspace(session, user_id, f'PG list {number}')
            runtime = OpenCodeRuntime(workspace_id=workspace.id, user_id=user_id,
                                      container_id=f'{number + 1:064x}', status='stopped')
            session.add(runtime); session.flush()
            session.add(McpRuntimeToken(runtime_id=runtime.id, user_id=user_id,
                workspace_id=workspace.id, jti=f'pg-list-{number}', token_hash=f'{number + 1:064x}',
                token_preview='hash-only', expires_at=beijing_now() + timedelta(hours=1)))
            expected[workspace.id] = runtime.id
            snapshot.append(dict(id=runtime.container_id, runtime_id=runtime.id,
                                 workspace_id=workspace.id, user_id=user_id, status='running'))
        session.commit(); session.expire_all()
        def observe(_):
            if state == 'recreated':
                # An independent connection commits while the list transaction is open.
                with Session(postgres) as writer:
                    writer.execute(update(OpenCodeRuntime).where(OpenCodeRuntime.user_id == user_id)
                        .values(container_id='f' * 64, status='running', config_version=7))
                    writer.commit()
            return snapshot if state == 'running' else []
        controller = Mock(); controller.user_snapshot.side_effect = observe
        queries = []
        def record(conn, cursor, statement, parameters, context, executemany):
            # Record only the list connection, excluding the concurrent writer.
            if conn is connection:
                queries.append(statement)
        connection = session.connection()
        event.listen(postgres, 'before_cursor_execute', record)
        try:
            rows = get_user_workspace_rows(session, user_id)
            with patch('app.features.workspace.docker_service._client', return_value=controller):
                result = list_summaries(session, rows, user_id)
        finally:
            event.remove(postgres, 'before_cursor_execute', record)
        assert len(result) == 25
        assert len([sql for sql in queries if sql.lstrip().upper().startswith('SELECT')]) == 1
        assert len(queries) <= 3, queries
        controller.user_snapshot.assert_called_once_with(user_id)
        session.expire_all()
        runtimes = session.query(OpenCodeRuntime).all()
        tokens = session.query(McpRuntimeToken).all()
        assert all((token.revoked_at is not None) == (state == 'missing') for token in tokens)
        if state == 'recreated':
            assert all(runtime.container_id == 'f' * 64 and runtime.config_version == 7
                       and runtime.status == 'running' for runtime in runtimes)
        elif state == 'running':
            assert all(runtime.status == 'running' and runtime.container_id for runtime in runtimes)
        else:
            assert all(runtime.status == 'stopped' and runtime.container_id is None for runtime in runtimes)


def test_pending_candidate_observation_preserves_operation_and_identity(postgres):
    from app.features.workspace import runtime_operation_repository as operations
    from app.models.runtime_operation import RuntimeOperation
    upgrade(postgres)
    with Session(postgres) as session:
        user = User(username='pending-start',password_hash='isolated-test-hash',role='common')
        session.add(user); session.commit()
        workspace = create_workspace(session,user.id,'Pending start')
        operation = operations.admit(session,workspace.id,user.id)
        session.commit()
        operations.record_candidate(session,operation.generation,'b'*64)
        session.commit()
        runtime = operations.switch_candidate(session,operation.generation)
        session.commit()
        rows = get_user_workspace_rows(session,user.id)
        client = Mock(); client.user_snapshot.return_value=[]
        with patch('app.features.workspace.docker_service._client',return_value=client):
            list_summaries(session,rows,user.id)
        session.expire_all()
        assert runtime.container_id == 'b'*64 and runtime.status == 'provisioning'
        assert session.get(RuntimeOperation,operation.generation).state == 'switched'
