"""Actual PostgreSQL rejected usage write must not emit bound prompt data."""
import os
import pytest
from loguru import logger
from sqlalchemy.orm import sessionmaker
from tests.integration.test_migrations import postgres
from entry.migrate import upgrade
from app.features.token_usage import service
from app.models.user import User
from app.models.token_usage_log import TokenUsageLog

pytestmark=pytest.mark.skipif(os.getenv('RUN_USAGE_DIAGNOSTIC_TESTS')!='1',reason='Opt-in actual PostgreSQL failure diagnostics')

def test_rejected_write_rolls_back_counters_and_keeps_sql_binds_private(postgres,monkeypatch):
    upgrade(postgres)
    factory=sessionmaker(bind=postgres)
    with factory() as seed:
        user=User(username='usage-log-owner',password_hash='isolated-test',role='common')
        seed.add(user);seed.commit();uid=user.id
    monkeypatch.setattr(service,'SessionLocal',factory)
    secret='private-usage-prompt-sentinel'
    captured=[]
    sink=logger.add(lambda message:captured.append((str(message),message.record['exception'])),format='{message}')
    try:
        # PostgreSQL varchar(50) rejects this action during INSERT, after UPDATE.
        service.record_usage(uid,'x'*51,prompt_tokens=2,completion_tokens=3,query_summary=secret,extra_info=secret)
    finally:logger.remove(sink)
    assert any('error_type=DataError' in message for message,_ in captured)
    assert all(secret not in message and exception is None for message,exception in captured)
    with factory() as observer:
        assert observer.query(TokenUsageLog).count()==0
        user=observer.get(User,uid)
        assert user.total_tokens==user.total_prompt_tokens==user.total_completion_tokens==0
