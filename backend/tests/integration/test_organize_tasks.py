from typing import Any
# pyright: reportPrivateUsage=false
"""真实整理器准入/投递/租约/隔离与进程崩溃恢复测试."""
import concurrent.futures
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import threading
import time

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.features.folder.organize.tasks import (
    admit_task, assert_task_claim, deliver_pending_organize_tasks,
    task_execution, RetryTaskLater,
)
from app.infra.task_queue import RabbitMQTaskConsumer, ORGANIZE_FILE_QUEUE, publish_organize_task as real_publish
from app.models.organize_task import OrganizeTask
from tests.integration.test_database_init import postgres  # noqa: F401

__all__ = ["postgres", "broker"]
from entry.init_db import initialize_database

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_ORGANIZE_TASK_TESTS") != "1", reason="Opt-in isolated organizer control"
)


@pytest.fixture
def tasks(postgres: Any):
    initialize_database(postgres)
    with postgres.begin() as db:
        db.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (1,'organizer','synthetic','common')"))
        db.execute(text("INSERT INTO workspaces(id,name,owner_id) VALUES (1,'organize-space',1)"))
        db.execute(text("INSERT INTO workspace_members(workspace_id,user_id,role) VALUES (1,1,'admin')"))
    return sessionmaker(bind=postgres)


def admitted(factory: Any):
    with factory() as db:
        assert admit_task(db,1,1)
        return db.query(OrganizeTask).one().id


def test_concurrent_admission_commits_one_active_task(tasks: Any) -> None:
    barrier = threading.Barrier(4)
    def submit(_: Any):
        barrier.wait(timeout=5)
        with tasks() as db:
            return admit_task(db,1,1)
    with concurrent.futures.ThreadPoolExecutor(4) as executor:
        assert sum(executor.map(submit,range(4))) == 1
    with tasks() as db:
        assert db.query(OrganizeTask).count() == 1


def test_terminal_replay_skips_and_live_executor_is_exclusive(tasks: Any) -> None:
    task_id = admitted(tasks)
    with task_execution(1,1,task_id,session_factory=tasks) as claim:
        with pytest.raises(RetryTaskLater):
            with task_execution(1,1,task_id,session_factory=tasks):
                pass
        with tasks() as db:
            row = assert_task_claim(db,task_id,claim,1,1)
            row.state = "completed"
            row.finished_at = datetime.now(timezone.utc)
            db.commit()
    with task_execution(1,1,task_id,session_factory=tasks) as claim:
        assert claim is None


def test_failed_claim_replacement_cannot_write(tasks: Any) -> None:
    from app.exceptions import PermissionDeniedError
    task_id = admitted(tasks)
    with task_execution(1,1,task_id,session_factory=tasks) as original:
        pass
    with task_execution(1,1,task_id,session_factory=tasks) as current:
        assert current != original
        with tasks() as db:
            with pytest.raises(PermissionDeniedError):
                assert_task_claim(db,task_id,original,1,1)
        with tasks() as db:
            assert_task_claim(db,task_id,current,1,1)


def test_broker_refusal_is_retryable(tasks: Any, request: Any, monkeypatch: Any) -> None:
    # 仅在需要 broker 的测试中解析共享真实 RabbitMQ fixture.
    request.getfixturevalue("broker")
    task_id = admitted(tasks)
    port = os.environ["RABBITMQ_PORT"]
    monkeypatch.setenv("RABBITMQ_PORT","1")
    assert deliver_pending_organize_tasks(session_factory=tasks,publisher=real_publish) == 0
    with tasks() as db:
        row = db.get(OrganizeTask,task_id)
        assert row.state == "queued" and row.delivered_at is None
        assert row.attempts == 1 and row.last_error_type
        row.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    monkeypatch.setenv("RABBITMQ_PORT",port)
    assert deliver_pending_organize_tasks(session_factory=tasks,publisher=real_publish) == 1
    consumer = RabbitMQTaskConsumer()
    try:
        message = consumer.get_message(ORGANIZE_FILE_QUEUE)
        assert message is not None
        assert json.loads(message.body) == {"workspace_id":1,"user_id":1,"lock_token":task_id}
        consumer.acknowledge(message)
    finally:
        consumer.close()


DEAD_PROCESS = """
import sys,time
from app.infra.task_queue import RabbitMQTaskConsumer, ORGANIZE_FILE_QUEUE
from app.features.folder.organize.tasks import task_execution
consumer = RabbitMQTaskConsumer()
message = consumer.get_message(ORGANIZE_FILE_QUEUE)
assert message is not None
with task_execution(1,1,sys.argv[1]) as claim:
    print('CLAIM_READY:' + claim,flush=True)
    while True:
        time.sleep(.1)
"""


def test_killed_process_releases_workspace_lease_and_unacked_delivery(tasks: Any, postgres: Any, request: Any) -> None:
    request.getfixturevalue("broker")
    task_id = admitted(tasks)
    assert deliver_pending_organize_tasks(session_factory=tasks,publisher=real_publish) == 1
    env = {**os.environ,"DATABASE_URL":postgres.url.render_as_string(hide_password=False)}
    child = subprocess.Popen([sys.executable,"-u","-c",DEAD_PROCESS,task_id],
        cwd=Path(__file__).resolve().parents[2],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    consumer = RabbitMQTaskConsumer()
    try:
        assert child.stdout is not None and child.stderr is not None
        ready, _, _ = select.select([child.stdout],[],[],10)
        assert ready, "Disposable child did not acquire its lease"
        line = child.stdout.readline().strip()
        assert line.startswith("CLAIM_READY:"), "Disposable child failed before lease acquisition"
        old_claim = line.split(":",1)[1]
        child.kill()
        child.wait(timeout=5)
        message = None
        deadline = time.monotonic() + 5
        while message is None and time.monotonic() < deadline:
            message = consumer.get_message(ORGANIZE_FILE_QUEUE)
            if message is None:
                time.sleep(.05)
        assert message is not None and json.loads(message.body)["lock_token"] == task_id
        with task_execution(1,1,task_id,session_factory=tasks) as claim:
            assert claim != old_claim
            with tasks() as db:
                row = assert_task_claim(db,task_id,claim,1,1)
                row.state = "completed"
                db.commit()
        consumer.acknowledge(message)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        if child.stdout:
            child.stdout.close()
        if child.stderr:
            child.stderr.close()
        consumer.close()


# 共享自选 broker fixture, 在定义后导入以保持此模块
# 专注于整理器控制并避免影响任何现存部署资源.
from tests.integration.test_task_queue import broker  # noqa: E402,F401
