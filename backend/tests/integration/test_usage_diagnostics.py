from typing import Any
# pyright: reportPrivateUsage=false
"""实际 PostgreSQL 拒绝用量写入时绝不泄漏绑定提示词数据测试."""
import os
import pytest
from loguru import logger
from sqlalchemy.orm import sessionmaker
from tests.integration.test_migrations import postgres

__all__ = ["postgres"]
from entry.migrate import upgrade
from app.features.token_usage import service
from app.models.user import User
from app.models.token_usage_log import TokenUsageLog

pytestmark=pytest.mark.skipif(os.getenv('RUN_USAGE_DIAGNOSTIC_TESTS')!='1',reason='Opt-in actual PostgreSQL failure diagnostics')

def test_rejected_write_rolls_back_counters_and_keeps_sql_binds_private(postgres: Any,monkeypatch: Any) -> None:
    upgrade(postgres)
    factory=sessionmaker(bind=postgres)
    with factory() as seed:
        user=User(username='usage-log-owner',password_hash='isolated-test',role='common')
        seed.add(user);seed.commit();uid=user.id
    monkeypatch.setattr(service,'SessionLocal',factory)
    secret='private-usage-prompt-sentinel'
    captured: list[tuple[str, Any]] = []

    def sink_handler(message: Any) -> None:
        captured.append((str(message), message.record['exception']))

    sink = logger.add(sink_handler, format='{message}')
    try:
        # 在 UPDATE 之后的 INSERT 期间, PostgreSQL varchar(50) 拒绝此操作.
        service.record_usage(service.RecordUsageParams(user_id=uid, action='x'*51, prompt_tokens=2,completion_tokens=3,query_summary=secret,extra_info=secret))
    finally:
        logger.remove(sink)
    assert any('error_type=DataError' in message for message, _ in captured)
    assert all(secret not in message and exception is None for message, exception in captured)
    with factory() as observer:
        assert observer.query(TokenUsageLog).count() == 0
        observed_user = observer.get(User, uid)
        assert observed_user is not None
        assert observed_user.total_tokens == observed_user.total_prompt_tokens == observed_user.total_completion_tokens == 0
