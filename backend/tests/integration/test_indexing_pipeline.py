"""Actual upload -> RabbitMQ -> locked SDK worker -> MinIO/PostgreSQL indexing."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from sqlalchemy import text

from tests.integration.test_migrations import postgres  # noqa: F401
from tests.integration.test_task_queue import broker  # noqa: F401
from tests.integration.test_upload_storage import minio_storage, upload_api  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INDEXING_PIPELINE_TESTS") != "1", reason="Opt-in isolated indexing pipeline"
)


@pytest.fixture
def provider(request):
    mode = request.param
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, body))
            if self.path.endswith("/chat/completions"):
                response = {
                    "id": "synthetic-description", "object": "chat.completion",
                    "created": 1, "model": "pipeline-chat",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "Synthetic searchable document"},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
                }
            elif self.path.endswith("/embeddings"):
                if mode == "rejected":
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"error":{"message":"synthetic rejection","type":"invalid_request_error"}}')
                    return
                inputs = body["input"]
                inputs = inputs if isinstance(inputs, list) else [inputs]
                response = {
                    "object": "list", "model": "pipeline-embedding",
                    "data": [{"object": "embedding", "index": i, "embedding": [1.0] + [0.0] * 1023}
                             for i in reversed(range(len(inputs)))],
                    "usage": {"prompt_tokens": 6, "total_tokens": 6},
                }
                if mode == "incomplete":
                    response["data"] = []
                if mode == "zero":
                    for item in response["data"]:
                        item["embedding"] = [0.0] * 1024
            else:
                self.send_error(404)
                return
            data = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield mode, "http://127.0.0.1:" + str(server.server_port) + "/v1", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


PROBE = """
import json, time
from app.infra.task_queue import RabbitMQTaskConsumer, FILE_PROCESS_QUEUE
from app.infra.worker_runtime import WorkerRuntime, worker_step
from app.infra.llm.sync_client import OpenAI
from app.infra.file_task_outbox import deliver_pending_file_tasks
assert OpenAI.__module__.startswith('openai')
assert deliver_pending_file_tasks() >= 1
consumer = RabbitMQTaskConsumer(.02)
runtime = WorkerRuntime(1)
inflight = {}
try:
    deadline = time.monotonic() + 30
    started = False
    while time.monotonic() < deadline:
        worker_step(runtime, consumer, inflight)
        started = started or bool(inflight)
        ready = consumer._channel.queue_declare(queue=FILE_PROCESS_QUEUE, passive=True).method.message_count
        if started and not inflight and ready == 0:
            break
    else:
        raise AssertionError('Indexing worker did not settle')
    print('PIPELINE_FINISHED')
finally:
    consumer.close()
    runtime.shutdown()
"""


@pytest.mark.parametrize("provider", ["success", "rejected", "incomplete", "zero"], indirect=True)
def test_actual_indexing_outcomes(upload_api, postgres, minio_storage, broker, provider, tmp_path):
    client, headers, _ = upload_api
    mode, endpoint, calls = provider
    source = b"Real object bytes become searchable chunks through the indexing worker."
    response = client.post("/api/files", headers=headers,
                           files={"file": ("pipeline.txt", source, "text/plain")})
    assert response.status_code == 201, response.text
    file_id = response.json()["id"]
    from app.infra.task_queue import publish_messages, FILE_PROCESS_QUEUE
    if mode == "success":
        # A second delivery of the same file must replace the existing chunks.
        publish_messages(FILE_PROCESS_QUEUE, [file_id])
    access, secret = minio_storage.integration_auth
    temporary = tmp_path / "worker-tmp"
    temporary.mkdir()
    env = {
        **os.environ,
        "DATABASE_URL": postgres.url.render_as_string(hide_password=False),
        "MINIO_ENDPOINT": minio_storage.integration_endpoint,
        "MINIO_ACCESS_KEY": access, "MINIO_SECRET_KEY": secret,
        "MINIO_BUCKET": minio_storage._bucket, "MINIO_SECURE": "false",
        "CHAT_API_URL": endpoint, "CHAT_API_KEY": "synthetic-pipeline-key",
        "CHAT_API_MODEL": "pipeline-chat",
        "EMB_API_URL": endpoint, "EMB_API_KEY": "synthetic-pipeline-key",
        "EMB_MODEL_NAME": "pipeline-embedding",
        "TMPDIR": str(temporary),
    }
    result = subprocess.run([sys.executable, "-c", PROBE],
                            cwd=Path(__file__).resolve().parents[2], env=env,
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stderr[-5000:]
    assert "PIPELINE_FINISHED" in result.stdout
    assert list(temporary.iterdir()) == []
    with postgres.connect() as db:
        row = db.execute(text("SELECT status, description, vector_dims(vector_info) FROM files WHERE id=:id"),
                         {"id": file_id}).one()
        chunks = db.execute(text("SELECT content,vector_dims(vector_info) FROM file_chunks WHERE file_id=:id"),
                            {"id": file_id}).all()
        if mode == "success":
            assert row == ("success", "Synthetic searchable document", 1024)
            assert len(chunks) == 1 and chunks[0] == (source.decode(), 1024)
            assert sum(path.endswith("/chat/completions") for path, _ in calls) == 2
            assert db.scalar(text("SELECT count(*) FROM token_usage_logs WHERE action='embedding'")) == 4
        else:
            assert row.status == "fail"
            assert not chunks
            assert db.scalar(text("SELECT count(*) FROM inbox WHERE title='文件处理失败'")) == 1
    assert any(path.endswith("/embeddings") for path, _ in calls)
    assert client.get(f"/api/files/{file_id}/download", headers=headers).content == source
