"""Actual API/queue/LangGraph/model-SDK/tools/SQL organization workflow."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from sqlalchemy import text

from tests.integration.test_migrations import postgres  # noqa: F401
from tests.integration.test_task_queue import broker  # noqa: F401
from tests.integration.test_upload_storage import minio_storage, upload_api  # noqa: F401
from tests.integration.test_worker_outcomes import redis_client  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_ORGANIZE_PIPELINE_TESTS") != "1", reason="Opt-in isolated complete organize workflow"
)

PROBE = """
import time
from app.infra.task_queue import RabbitMQTaskConsumer, ORGANIZE_FILE_QUEUE
from app.infra.worker_runtime import WorkerRuntime, worker_step
from app.features.folder.organize.handler import ChatOpenAI, create_react_agent
assert ChatOpenAI.__module__.startswith('langchain_openai')
assert create_react_agent.__module__.startswith('langgraph')
consumer = RabbitMQTaskConsumer(.02)
runtime = WorkerRuntime(1)
inflight = {}
try:
    deadline = time.monotonic() + 25
    started = False
    while time.monotonic() < deadline:
        worker_step(runtime, consumer, inflight)
        started = started or bool(inflight)
        ready = consumer._channel.queue_declare(queue=ORGANIZE_FILE_QUEUE, passive=True).method.message_count
        if started and not inflight and ready == 0:
            break
    else:
        raise AssertionError('Organize worker did not settle')
    print('ORGANIZE_PIPELINE_FINISHED')
finally:
    consumer.close()
    runtime.shutdown()
"""


@pytest.mark.parametrize("mode", ["success", "provider_rejected", "checkpoint_rejected"])
def test_complete_organize_workflow(upload_api, postgres, minio_storage, broker, redis_client, mode):
    client, headers, _ = upload_api
    content = b"Synthetic document that belongs in the organized folder."
    uploaded = client.post("/api/files", headers=headers,
                           files={"file": ("organize.txt", content, "text/plain")})
    assert uploaded.status_code == 201, uploaded.text
    file_id = uploaded.json()["id"]
    with postgres.connect() as db:
        root_id = db.scalar(text("SELECT id FROM folder WHERE workspace_id=1 AND parent_id IS NULL"))
    if mode == "checkpoint_rejected":
        with postgres.begin() as db:
            db.execute(text("""
                CREATE FUNCTION reject_checkpoint() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN RAISE EXCEPTION 'synthetic checkpoint failure'; END $$
            """))
            db.execute(text("CREATE TRIGGER reject_checkpoint BEFORE INSERT OR UPDATE ON organize_checkpoints FOR EACH ROW EXECUTE FUNCTION reject_checkpoint()"))
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            assert self.path == "/v1/chat/completions"
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(request)
            assert not request.get("stream")
            if mode == "provider_rejected":
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":{"message":"synthetic provider rejection","type":"invalid_request_error"}}')
                return
            turn = len(calls) - 1
            message = {"role":"assistant", "content":None}
            if turn == 0:
                name, args = "get_all_files", {"workspace_id":1}
            elif turn == 1:
                name, args = "create_folder", {"name":"organized","parent_id":root_id,"workspace_id":1}
            elif turn == 2:
                tool_messages = [item for item in request["messages"] if item["role"] == "tool"]
                folder_id = int(re.search(r"ID: (\d+)", tool_messages[-1]["content"]).group(1))
                name, args = "move_file", {"file_id":file_id,"target_folder_id":folder_id,"workspace_id":1}
            else:
                name, args = None, None
                message["content"] = "Organization complete."
            if name:
                message["tool_calls"] = [{
                    "id":"synthetic-tool-" + str(turn), "type":"function",
                    "function":{"name":name,"arguments":json.dumps(args)},
                }]
            payload = {"id":"synthetic-organize-" + str(turn), "object":"chat.completion",
                       "created":1,"model":"pipeline-organizer",
                       "choices":[{"index":0,"message":message,
                                   "finish_reason":"tool_calls" if name else "stop"}],
                       "usage":{"prompt_tokens":5,"completion_tokens":3,"total_tokens":8}}
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(("127.0.0.1",0),Handler)
    thread = threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        queued = client.post("/api/folder/organize", headers=headers)
        assert queued.status_code == 200 and queued.json()["queued"] is True
        assert client.post("/api/folder/organize", headers=headers).json()["queued"] is False
        assert redis_client.get("organize:task:lock:1").endswith(":queued")
        env = {**os.environ, "DATABASE_URL":postgres.url.render_as_string(hide_password=False),
               "REDIS_PORT":str(redis_client.connection_pool.connection_kwargs["port"]),
               "CHAT_API_URL":f"http://127.0.0.1:{server.server_port}/v1",
               "CHAT_API_KEY":"synthetic-pipeline-model-key",
               "CHAT_API_MODEL":"pipeline-organizer"}
        result = subprocess.run([sys.executable,"-c",PROBE],
            cwd=Path(__file__).resolve().parents[2],env=env,capture_output=True,text=True,timeout=35)
        assert result.returncode == 0, result.stderr[-5000:]
        assert "ORGANIZE_PIPELINE_FINISHED" in result.stdout
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert redis_client.get("organize:task:lock:1") is None
    with postgres.connect() as db:
        message = db.scalar(text("SELECT content FROM inbox WHERE user_id=1"))
        assert db.scalar(text("SELECT count(*) FROM inbox WHERE user_id=1")) == 1
        if mode == "success":
            folder = db.execute(text("SELECT id,parent_id FROM folder WHERE name='organized' AND workspace_id=1")).one()
            assert folder.parent_id == root_id
            assert db.scalar(text("SELECT parent_id FROM files WHERE id=:id"),{"id":file_id}) == folder.id
            assert "校验通过" in message and "checkpoint" in message
            assert db.scalar(text("SELECT last_event_id FROM organize_checkpoints WHERE workspace_id=1")) > 0
            assert db.scalar(text("SELECT sum(total_tokens) FROM token_usage_logs WHERE action='organize'")) == 32
            assert db.scalar(text("SELECT count(*) FROM token_usage_logs WHERE action='organize'")) == 4
            assert len(calls) == 4
        else:
            assert "执行失败" in message
            assert db.scalar(text("SELECT count(*) FROM organize_checkpoints")) == 0
            if mode == "checkpoint_rejected":
                assert len(calls) == 4
                assert "synthetic checkpoint failure" in result.stderr
                assert db.scalar(text("SELECT sum(total_tokens) FROM token_usage_logs WHERE action='organize'")) == 32
                folder_id = db.scalar(text("SELECT id FROM folder WHERE name='organized' AND workspace_id=1"))
                assert folder_id is not None
                assert db.scalar(text("SELECT parent_id FROM files WHERE id=:id"),{"id":file_id}) == folder_id

            if mode == "provider_rejected":
                assert db.scalar(text("SELECT parent_id FROM files WHERE id=:id"),{"id":file_id}) == root_id
    assert client.get(f"/api/files/{file_id}/download",headers=headers).content == content


def test_concurrent_checkpoint_creation_and_updates_never_regress(postgres):
    import concurrent.futures
    from sqlalchemy.orm import sessionmaker
    from app.features.folder.change_log import update_checkpoint
    from entry.migrate import upgrade

    upgrade(postgres)
    with postgres.begin() as db:
        db.execute(text("INSERT INTO users(id,username,password_hash,role) VALUES (1,'checkpoint-user','synthetic','common')"))
        db.execute(text("INSERT INTO workspaces(id,name,owner_id) VALUES (1,'checkpoint-space',1)"))
    factory = sessionmaker(bind=postgres)
    barrier = threading.Barrier(5)
    def update(value):
        barrier.wait(timeout=5)
        with factory() as db:
            update_checkpoint(db, 1, value, mark_full_scan=(value == 4))
    with concurrent.futures.ThreadPoolExecutor(5) as executor:
        list(executor.map(update,[50,4,12,1,31]))
    with factory() as db:
        update_checkpoint(db, 1, 2)
        row = db.execute(text("SELECT last_event_id,last_full_scan_at FROM organize_checkpoints WHERE workspace_id=1")).one()
        assert row.last_event_id == 50 and row.last_full_scan_at is not None
        assert db.scalar(text("SELECT count(*) FROM organize_checkpoints")) == 1
