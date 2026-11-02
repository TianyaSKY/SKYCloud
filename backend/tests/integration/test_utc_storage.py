"""UTC 存储保持用量按北京时间日历日分组测试."""

from datetime import datetime, timezone
import os
from unittest.mock import patch

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.features.token_usage import service as usage
from app.models.user import User
from entry.migrate import upgrade
from tests.integration.test_migrations import postgres  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_UTC_STORAGE_TESTS") != "1",
    reason="Opt-in isolated PostgreSQL UTC storage behavior",
)


@pytest.mark.parametrize("database_zone", ["UTC", "America/New_York"])
def test_usage_days_remain_beijing_days_with_utc_storage(postgres, database_zone):
    upgrade(postgres)
    with postgres.connect() as connection:
        connection.execute(
            text("SELECT set_config('TimeZone', :zone, false)"), {"zone": database_zone}
        )
        connection.commit()
        with Session(connection) as session:
            session.add(
                User(id=1, username="daily-clock", password_hash="synthetic", role="common")
            )
            session.commit()
            session.execute(
                text(
                    """INSERT INTO token_usage_logs(user_id,action,total_tokens,created_at)
                    VALUES (1,'chat',5,'2025-05-31 17:00:00+00')"""
                )
            )
            session.commit()
            with patch.object(
                usage,
                "utc_now",
                return_value=datetime(2025, 6, 1, 4, tzinfo=timezone.utc),
            ):
                for query, args in (
                    (usage.get_daily_stats, (session, 1)),
                    (usage.get_all_users_daily_stats, (session,)),
                    (usage.get_per_user_daily_stats, (session,)),
                ):
                    result = query(*args)
                    assert str(result[0]["date"]) == "2025-06-01"
                    assert result[0]["total_tokens"] == 5

            naive_columns = session.scalar(
                text(
                    """SELECT count(*) FROM information_schema.columns
                    WHERE table_schema=current_schema()
                    AND data_type='timestamp without time zone'"""
                )
            )
            assert naive_columns == 0
