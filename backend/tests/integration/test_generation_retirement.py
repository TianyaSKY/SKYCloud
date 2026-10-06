"""Completed generations must retire after their last reference, including history."""
import os
import pytest
from sqlalchemy import text, inspect
from entry.migrate import upgrade
from app.infra.schema import check_schema
from tests.integration.test_migrations import postgres

pytestmark = pytest.mark.skipif(os.getenv('RUN_MIGRATION_TESTS') != '1', reason='Opt-in isolated PostgreSQL migrations')


def test_completed_generation_backfill_and_last_reference(postgres):
    upgrade(postgres,'0015_runtime_operations')
    ids = {'orphan':'a'*64,'current':'b'*64,'shared':'c'*64}
    with postgres.begin() as db:
        db.execute(text("INSERT INTO users(id,username,password_hash) VALUES (1,'generation','x')"))
        db.execute(text("INSERT INTO workspaces(id,name,owner_id,container_id) VALUES (1,'generation',1,:shared)"),ids)
        db.execute(text("INSERT INTO opencode_runtimes(id,workspace_id,user_id,container_id,status) VALUES (1,1,1,:current,'running')"),ids)
        for n,key in enumerate(ids,1):
            db.execute(text("INSERT INTO runtime_operations(generation,workspace_id,user_id,runtime_id,candidate_container_id,state,finished_at) VALUES (:g,1,1,1,:cid,'complete',now())"),{'g':str(n)*32,'cid':ids[key]})
        db.execute(text("INSERT INTO runtime_container_deletions(container_id,completed_at) VALUES (:orphan,now())"),ids)
    upgrade(postgres)
    upgrade(postgres)
    check_schema(postgres)
    with postgres.begin() as db:
        state=lambda n: db.execute(text('SELECT state,finished_at IS NULL FROM runtime_operations WHERE generation=:g'),{'g':str(n)*32}).one()
        assert [state(n) for n in (1,2,3)] == [('cancelled',True),('complete',False),('complete',False)]
        assert db.scalar(text('SELECT completed_at IS NOT NULL FROM runtime_container_deletions'))
        assert 'ix_runtime_operations_candidate' in {x['name'] for x in inspect(db).get_indexes('runtime_operations')}
        db.execute(text("UPDATE opencode_runtimes SET status='stopped' WHERE id=1"))
        assert state(2) == ('complete',False)
        savepoint=db.begin_nested()
        db.execute(text('UPDATE opencode_runtimes SET container_id=NULL WHERE id=1'))
        assert state(2) == ('cancelled',True)
        savepoint.rollback()
        assert state(2) == ('complete',False)
        db.execute(text('UPDATE opencode_runtimes SET container_id=:shared WHERE id=1'),ids)
        assert state(2) == ('cancelled',True)
        db.execute(text('DELETE FROM opencode_runtimes WHERE id=1'))
        assert state(3) == ('complete',False)
        db.execute(text('DELETE FROM users WHERE id=1'))
        assert state(3) == ('cancelled',True)


def test_retirement_migration_failure_is_atomic(postgres):
    upgrade(postgres,'0015_runtime_operations')
    with postgres.begin() as db:
        db.execute(text("INSERT INTO runtime_operations(generation,workspace_id,user_id,runtime_id,candidate_container_id,state,finished_at) VALUES (:g,1,1,1,:cid,'complete',now())"),{'g':'a'*32,'cid':'b'*64})
        db.execute(text("CREATE FUNCTION retire_detached_completed_generation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN OLD; END $$"))
    with pytest.raises(Exception,match='already exists'):
        upgrade(postgres)
    with postgres.begin() as db:
        assert db.scalar(text('SELECT version_num FROM alembic_version')) == '0015_runtime_operations'
        assert db.execute(text('SELECT state,finished_at IS NOT NULL FROM runtime_operations')).one() == ('complete',True)
        assert db.scalar(text("SELECT to_regclass('ix_runtime_operations_candidate')")) is None
        db.execute(text('DROP FUNCTION retire_detached_completed_generation()'))
    upgrade(postgres)
    with postgres.connect() as db:
        assert db.execute(text('SELECT state,finished_at IS NULL FROM runtime_operations')).one() == ('cancelled',True)


@pytest.mark.skipif(os.getenv('RUN_CONTAINER_DELETION_TESTS') != '1', reason='Opt-in authenticated Controller/Docker')
def test_historical_completed_cleanup_retires_generation_after_upgrade(postgres,infrastructure,controller):
    import uuid
    from sqlalchemy.orm import sessionmaker
    from app.infra.runtime_operation_cleanup import retire_cancelled_operations
    from runtime_controller.client import ControllerError
    upgrade(postgres,'0015_runtime_operations')
    identity=int(uuid.uuid4().hex[:6],16)+1000
    generation=uuid.uuid4().hex
    infrastructure['cleanup'].append(f'skycloud-opencode-w{identity}-u{identity}-g{generation}')
    candidate=controller['client'].prepare(identity,identity,identity,generation)
    with postgres.begin() as db:
        db.execute(text("INSERT INTO users(id,username,password_hash) VALUES (:id,'historical','x')"),{'id':identity})
        db.execute(text("INSERT INTO workspaces(id,name,owner_id) VALUES (:id,'historical',:id)"),{'id':identity})
        db.execute(text("INSERT INTO opencode_runtimes(id,workspace_id,user_id,container_id,status) VALUES (:id,:id,:id,:cid,'running')"),{'id':identity,'cid':candidate.id})
        db.execute(text("INSERT INTO runtime_operations(generation,workspace_id,user_id,runtime_id,candidate_container_id,state,finished_at) VALUES (:g,:id,:id,:id,:cid,'complete',now())"),{'g':generation,'id':identity,'cid':candidate.id})
        db.execute(text('UPDATE opencode_runtimes SET container_id=NULL WHERE id=:id'),{'id':identity})
        db.execute(text('UPDATE runtime_container_deletions SET completed_at=now()'))
    upgrade(postgres)
    factory=sessionmaker(bind=postgres)
    class Unavailable:
        def retire_generation(self,*args): raise ControllerError('unavailable')
    assert retire_cancelled_operations(session_factory=factory,controller_factory=Unavailable)==0
    assert controller['client'].get(candidate.id).id==candidate.id
    with postgres.begin() as db:
        assert db.execute(text('SELECT state,finished_at IS NULL,last_error_type FROM runtime_operations')).one()==('cancelled',True,'ControllerError')
        db.execute(text('UPDATE runtime_operations SET next_attempt_at=clock_timestamp()'))
    assert retire_cancelled_operations(session_factory=factory,controller_factory=lambda:controller['client'])==1
    with pytest.raises(ControllerError): controller['client'].prepare(identity,identity,identity,generation)
    from tests.integration.test_runtime_security import docker
    assert docker('inspect',candidate.id,check=False).returncode!=0


# Fixtures are imported explicitly so collection does not include their tests.
from tests.integration.test_runtime_security import infrastructure, controller
