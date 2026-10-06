"""UTC schema conversion must preserve legacy instants, NULLs and defaults atomically."""
from datetime import datetime, timezone
import os

from alembic import command
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from entry.migrate import migration_config, upgrade
from tests.integration.test_migrations import postgres  # noqa: F401
from app.infra.schema import check_schema
from app.models.user import User
from app.features.token_usage import service as usage

pytestmark = pytest.mark.skipif(os.getenv("RUN_UTC_MIGRATION_TESTS") != "1",
                               reason="Opt-in isolated PostgreSQL UTC conversion")


def seed_legacy(postgres):
    upgrade(postgres, "0008_organize_tasks")
    with postgres.begin() as db:
        db.execute(text("""INSERT INTO users(id,username,password_hash,role,created_at)
            VALUES (1,'old-clock','synthetic','common','2025-06-01 00:30:00')"""))
        db.execute(text("INSERT INTO workspaces(id,name,owner_id) VALUES (1,'old-space',1)"))
        db.execute(text("INSERT INTO files(id,name,file_path,file_size,workspace_id,uploader_id) VALUES (1,'clock.txt','synthetic',1,1,1)"))
        db.execute(text("""INSERT INTO shares(token,file_id,user_id,created_at,expires_at)
            VALUES ('old-share',1,1,'2025-06-01 00:30:00','2025-06-01 12:30:00')"""))
        db.execute(text("""INSERT INTO upload_reservations(id,user_id,workspace_id,size,kind,created_at,expires_at)
            VALUES ('utc-reservation',1,1,1,'file','2025-06-01 04:00:00','2025-06-01 05:00:00')"""))
        db.execute(text("""INSERT INTO file_task_outbox(file_id,created_at,next_attempt_at)
            VALUES (1,'2025-06-01 07:00:00+03','2025-06-01 07:00:00+03')"""))


@pytest.mark.parametrize("database_zone", ["UTC", "America/New_York"])
def test_historical_instants_defaults_and_all_columns(postgres, database_zone):
    seed_legacy(postgres)
    with postgres.begin() as db:
        db.execute(text("SELECT set_config('TimeZone', :zone, true)"), {"zone": database_zone})
        command.upgrade(migration_config(db), "head")
        assert db.scalar(text("SELECT created_at FROM users WHERE id=1")) == datetime(2025,5,31,16,30,tzinfo=timezone.utc)
        assert db.scalar(text("SELECT last_active_at FROM users WHERE id=1")) is None
        assert db.scalar(text("SELECT expires_at FROM shares WHERE token='old-share'")) == datetime(2025,6,1,4,30,tzinfo=timezone.utc)
        assert db.scalar(text("SELECT expires_at FROM upload_reservations")) == datetime(2025,6,1,5,tzinfo=timezone.utc)
        assert db.scalar(text("SELECT created_at FROM file_task_outbox")) == datetime(2025,6,1,4,tzinfo=timezone.utc)
        db.execute(text("INSERT INTO users(id,username,password_hash) VALUES (2,'default-clock','synthetic')"))
        assert db.scalar(text("SELECT created_at = now() FROM users WHERE id=2")) is True
        assert db.scalar(text("""SELECT count(*) FROM information_schema.columns
            WHERE table_schema=current_schema() AND data_type='timestamp without time zone'""")) == 0
    check_schema(postgres)
    with Session(postgres) as db:
        assert db.get(User,1).created_at.tzinfo == timezone.utc
    upgrade(postgres)
    with postgres.connect() as db:
        assert db.scalar(text("SELECT created_at FROM users WHERE id=1")) == datetime(2025,5,31,16,30,tzinfo=timezone.utc)


def test_conversion_failure_restores_columns_values_defaults_and_revision(postgres):
    seed_legacy(postgres)
    with postgres.begin() as db:
        db.execute(text("ALTER TABLE shares ALTER COLUMN expires_at TYPE TEXT"))
    with pytest.raises(RuntimeError, match="Unexpected timestamp type"):
        upgrade(postgres)
    with postgres.connect() as db:
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0008_organize_tasks"
        assert db.scalar(text("SELECT created_at FROM users WHERE id=1")) == datetime(2025,6,1,0,30)
        assert db.scalar(text("""SELECT data_type FROM information_schema.columns
            WHERE table_name='users' AND column_name='created_at'""")) == 'timestamp without time zone'
        assert 'Asia/Shanghai' in db.scalar(text("""SELECT column_default FROM information_schema.columns
            WHERE table_name='users' AND column_name='created_at'"""))


def test_already_aware_historical_column_is_not_shifted_again(postgres):
    seed_legacy(postgres)
    with postgres.begin() as db:
        db.execute(text("ALTER TABLE users ALTER COLUMN created_at DROP DEFAULT"))
        db.execute(text("ALTER TABLE users ALTER COLUMN created_at TYPE TIMESTAMPTZ USING created_at AT TIME ZONE 'UTC'"))
        db.execute(text("ALTER TABLE users ALTER COLUMN created_at SET DEFAULT now()"))
    upgrade(postgres)
    with postgres.connect() as db:
        assert db.scalar(text("SELECT created_at FROM users WHERE id=1")) == datetime(2025,6,1,0,30,tzinfo=timezone.utc)


@pytest.mark.parametrize("database_zone", ["UTC", "America/New_York"])
def test_usage_days_remain_beijing_days_with_utc_storage(postgres, database_zone):
    upgrade(postgres)
    with postgres.connect() as connection:
        connection.execute(text("SELECT set_config('TimeZone', :zone, false)"), {"zone": database_zone})
        connection.commit()
        with Session(connection) as db:
            db.add(User(id=1,username='daily-clock',password_hash='synthetic',role='common'))
            db.commit()
            db.execute(text("""INSERT INTO token_usage_logs(user_id,action,total_tokens,created_at)
                VALUES (1,'chat',5,'2025-05-31 17:00:00+00')"""))
            db.commit()
            from unittest.mock import patch
            with patch.object(usage,'utc_now',return_value=datetime(2025,6,1,4,tzinfo=timezone.utc)):
                for query,args in ((usage.get_daily_stats,(db,1)),(usage.get_all_users_daily_stats,(db,)),
                                   (usage.get_per_user_daily_stats,(db,))):
                    result = query(*args)
                    assert str(result[0]['date']) == '2025-06-01'
                    assert result[0]['total_tokens'] == 5
