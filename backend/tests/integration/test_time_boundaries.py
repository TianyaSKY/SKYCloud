"""Actual PostgreSQL timezone independence for legacy timestamp columns."""
import os

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from entry.migrate import upgrade
from tests.integration.test_migrations import postgres  # noqa: F401
from tests.test_time_boundaries import assert_share_and_usage_times
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember

pytestmark = pytest.mark.skipif(os.getenv("RUN_TIME_BOUNDARY_TESTS") != "1",
                               reason="Opt-in isolated PostgreSQL clock boundaries")


@pytest.mark.parametrize("database_zone", ["UTC", "America/New_York"])
def test_expiry_and_filters_ignore_database_session_timezone(postgres, database_zone):
    upgrade(postgres)
    with postgres.connect() as connection:
        connection.execute(text("SELECT set_config('TimeZone', :zone, false)"), {"zone": database_zone})
        connection.commit()
        with Session(bind=connection) as session:
            user = User(username="clock-user", password_hash="synthetic", role="common")
            session.add(user)
            session.flush()
            workspace = Workspace(name="clock-space", owner_id=user.id)
            session.add(workspace)
            session.flush()
            session.add(WorkspaceMember(workspace_id=workspace.id,user_id=user.id,role="admin"))
            session.commit()
            assert_share_and_usage_times(session, user.id, workspace.id)
