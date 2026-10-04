"""基础设施测试：task_queue 消费者 + llm/client + indexing handler。"""

import json
import pytest
from unittest.mock import patch, MagicMock, AsyncMock, call

from app.infra.task_queue import (
    QueueMessage,
    _rabbitmq_port,
    _connection_parameters,
    publish_messages,
    publish_file_tasks,
    publish_organize_task,
    RabbitMQTaskConsumer,
    FILE_PROCESS_QUEUE,
    ORGANIZE_FILE_QUEUE,
    TASK_QUEUES,
)


# ---------------------------------------------------------------------------
# task_queue 基础函数
# ---------------------------------------------------------------------------


class TestQueueMessage:
    def test_dataclass(self):
        msg = QueueMessage(queue_name="q", body="hello")
        assert msg.queue_name == "q"
        assert msg.body == "hello"


class TestRabbitmqPort:
    def test_default(self, monkeypatch):
        monkeypatch.delenv("RABBITMQ_PORT", raising=False)
        assert _rabbitmq_port() == 5672

    def test_custom(self, monkeypatch):
        monkeypatch.setenv("RABBITMQ_PORT", "5673")
        assert _rabbitmq_port() == 5673


class TestConnectionParameters:
    def test_url_based(self, monkeypatch):
        monkeypatch.setenv("RABBITMQ_URL", "amqp://user:explicit-test-queue-password@host:5672/vh")
        params = _connection_parameters()
        assert params is not None

    def test_host_based(self, monkeypatch):
        monkeypatch.delenv("RABBITMQ_URL", raising=False)
        monkeypatch.setenv("RABBITMQ_HOST", "myhost")
        monkeypatch.setenv("RABBITMQ_USER", "admin")
        monkeypatch.setenv("RABBITMQ_PASSWORD", "explicit-test-queue-password")
        params = _connection_parameters()
        assert params is not None


class TestPublishMessages:
    def test_publish(self):
        mock_conn = MagicMock()
        mock_channel = MagicMock()
        mock_conn.channel.return_value = mock_channel
        mock_conn.is_open = True

        with patch("app.infra.task_queue.open_connection", return_value=mock_conn):
            publish_messages("test_queue", [1, 2, 3])

        assert mock_channel.basic_publish.call_count == 3
        mock_conn.close.assert_called_once()


class TestPublishFileTasks:
    def test_delegates(self):
        with patch("app.infra.task_queue.publish_messages") as m:
            publish_file_tasks([1, 2])
            m.assert_called_once_with(FILE_PROCESS_QUEUE, [1, 2])


class TestPublishOrganizeTask:
    def test_delegates(self):
        with patch("app.infra.task_queue.publish_messages") as m:
            publish_organize_task(1, 2, "token123")
            args = m.call_args
            assert args[0][0] == ORGANIZE_FILE_QUEUE
            payload = json.loads(args[0][1][0])
            assert payload["workspace_id"] == 1
            assert payload["lock_token"] == "token123"


# ---------------------------------------------------------------------------
# RabbitMQTaskConsumer
# ---------------------------------------------------------------------------


class TestRabbitMQTaskConsumer:
    def test_init(self):
        consumer = RabbitMQTaskConsumer(poll_interval_seconds=1.0)
        assert consumer.poll_interval_seconds == 1.0
        assert consumer._connection is None

    def test_connect(self):
        mock_conn = MagicMock()
        mock_channel = MagicMock()
        mock_conn.channel.return_value = mock_channel

        consumer = RabbitMQTaskConsumer()
        with patch("app.infra.task_queue.open_connection", return_value=mock_conn), \
             patch("app.infra.task_queue.declare_task_queues"):
            consumer.connect()
        assert consumer._connection == mock_conn
        assert consumer._channel == mock_channel

    def test_close(self):
        consumer = RabbitMQTaskConsumer()
        mock_conn = MagicMock()
        mock_conn.is_open = True
        consumer._connection = mock_conn
        consumer._channel = MagicMock()
        consumer.close()
        mock_conn.close.assert_called_once()
        assert consumer._connection is None

    def test_close_already_closed(self):
        consumer = RabbitMQTaskConsumer()
        mock_conn = MagicMock()
        mock_conn.is_open = False
        consumer._connection = mock_conn
        consumer.close()
        mock_conn.close.assert_not_called()

    def test_decode_body_bytes(self):
        consumer = RabbitMQTaskConsumer()
        assert consumer._decode_body(b"hello") == "hello"

    def test_decode_body_str(self):
        consumer = RabbitMQTaskConsumer()
        assert consumer._decode_body("hello") == "hello"

    def test_get_message_found(self):
        consumer = RabbitMQTaskConsumer()
        mock_channel = MagicMock()
        mock_channel.basic_get.return_value = (MagicMock(), None, b"42")
        consumer._connection = MagicMock()
        consumer._connection.is_closed = False
        consumer._channel = mock_channel
        mock_channel.is_closed = False

        msg = consumer.get_message("test_q")
        assert msg is not None
        assert msg.body == "42"
        assert msg.queue_name == "test_q"

    def test_get_message_empty(self):
        consumer = RabbitMQTaskConsumer()
        mock_channel = MagicMock()
        mock_channel.basic_get.return_value = (None, None, None)
        consumer._connection = MagicMock()
        consumer._connection.is_closed = False
        consumer._channel = mock_channel
        mock_channel.is_closed = False

        msg = consumer.get_message("test_q")
        assert msg is None

    def test_drain_messages(self):
        consumer = RabbitMQTaskConsumer()
        mock_channel = MagicMock()
        # 返回 2 条消息后返回空
        mock_channel.basic_get.side_effect = [
            (MagicMock(), None, b"1"),
            (MagicMock(), None, b"2"),
            (None, None, None),
        ]
        consumer._connection = MagicMock()
        consumer._connection.is_closed = False
        consumer._channel = mock_channel
        mock_channel.is_closed = False

        messages = consumer.drain_messages("q", 10)
        assert len(messages) == 2

    def test_ensure_channel_reconnects(self):
        consumer = RabbitMQTaskConsumer()
        consumer._connection = None
        consumer._channel = None

        with patch.object(consumer, "connect") as mock_connect:
            mock_connect.side_effect = lambda: setattr(consumer, "_channel", MagicMock())
            channel = consumer._ensure_channel()
            mock_connect.assert_called_once()


# ---------------------------------------------------------------------------
# llm/client
# ---------------------------------------------------------------------------

from app.infra.llm.client import (
    _get_client,
    _safe_record,
    chat_completion,
    embed_texts,
    record_llm_usage,
)


class TestGetClient:
    def test_caching(self):
        from app.infra.llm import client
        with patch.dict(client._client_cache, {}, clear=True), patch("app.infra.llm.client.AsyncOpenAI") as MockOpenAI:
            MockOpenAI.return_value = MagicMock()
            c1 = _get_client("http://api", "key1")
            c2 = _get_client("http://api", "key1")
            assert c1 is c2
            assert MockOpenAI.call_count == 1


class TestSafeRecord:
    def test_success(self):
        with patch("app.features.token_usage.service.record_usage") as m:
            _safe_record(1, "chat", "model", 10, 20, 30)
            m.assert_called_once()

    def test_exception_swallowed(self):
        with patch("app.features.token_usage.service.record_usage", side_effect=Exception("err")):
            _safe_record(1, "chat", "model", 10, 20, 30)  # 不应抛异常


class TestRecordLlmUsage:
    def test_zero_tokens_skipped(self):
        with patch("app.infra.llm.client._safe_record") as m:
            record_llm_usage(user_id=1, action="chat", total_tokens=0, prompt_tokens=0)
            m.assert_not_called()

    def test_nonzero_recorded(self):
        with patch("app.infra.llm.client._safe_record") as m:
            record_llm_usage(user_id=1, action="chat", total_tokens=100, prompt_tokens=50)
            m.assert_called_once()


class TestChatCompletion:
    async def test_success(self):
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.usage.prompt_tokens = 10
        mock_resp.usage.completion_tokens = 20
        mock_resp.usage.total_tokens = 30
        mock_client.chat.completions.create = AsyncMock(return_value=mock_resp)

        with patch("app.infra.llm.client._get_client", return_value=mock_client), \
             patch("app.infra.llm.client._safe_record") as mock_record:
            result = await chat_completion(
                messages=[{"role": "user", "content": "hi"}],
                config={"api": "http://api", "key": "k", "model": "m"},
                user_id=1,
            )
        assert result == mock_resp
        mock_record.assert_called_once()

    async def test_no_usage(self):
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.usage = None
        mock_client.chat.completions.create = AsyncMock(return_value=mock_resp)

        with patch("app.infra.llm.client._get_client", return_value=mock_client), \
             patch("app.infra.llm.client._safe_record") as mock_record:
            result = await chat_completion(
                messages=[{"role": "user", "content": "hi"}],
                config={"api": "http://api", "key": "k", "model": "m"},
            )
        mock_record.assert_not_called()


class TestEmbedTexts:
    async def test_empty_texts(self):
        result = await embed_texts(texts=[], config={"api": "http://a", "key": "k", "model": "m"})
        assert result == []

    async def test_string_input(self):
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_item = MagicMock()
        mock_item.index = 0
        mock_item.embedding = [0.1] * 2048  # 超长向量截取前 1024
        mock_resp.data = [mock_item]
        mock_resp.usage.total_tokens = 10
        mock_resp.usage.prompt_tokens = 10
        mock_client.embeddings.create = AsyncMock(return_value=mock_resp)

        with patch("app.infra.llm.client._get_client", return_value=mock_client), \
             patch("app.infra.llm.client._safe_record"):
            result = await embed_texts(texts="hello", config={"api": "http://a", "key": "k", "model": "m"})
        assert len(result) == 1
        assert len(result[0]) == 1024

    async def test_multiple_texts(self):
        mock_client = MagicMock()
        mock_resp = MagicMock()
        items = []
        for i in range(3):
            item = MagicMock()
            item.index = i
            item.embedding = [0.1] * 1024
            items.append(item)
        mock_resp.data = items
        mock_resp.usage = None
        mock_client.embeddings.create = AsyncMock(return_value=mock_resp)

        with patch("app.infra.llm.client._get_client", return_value=mock_client):
            result = await embed_texts(texts=["a", "b", "c"], config={"api": "http://a", "key": "k", "model": "m"})
        assert len(result) == 3


# ---------------------------------------------------------------------------
# indexing handler
# ---------------------------------------------------------------------------

from app.infra.indexing.handler import (
    handle_file_indexing,
    _mark_file_failed,
    handle_file_process,
)


class TestHandleFileIndexing:
    def test_file_not_found(self):
        mock_session = MagicMock()
        from app.exceptions import ResourceNotFoundError
        with patch("app.infra.indexing.handler.SessionLocal", return_value=mock_session), \
             patch("app.infra.indexing.handler.file_service.get_file", side_effect=ResourceNotFoundError("not found")):
            handle_file_indexing(999)
        mock_session.close.assert_called_once()

    def test_success_flow(self, session, test_workspace, test_user, tmp_path):
        from sqlalchemy.orm import Session
        from app.models.file import File
        from app.models.file_chunk import FileChunk

        source = tmp_path / "test.txt"
        source.write_text("Indexable text about file indexing.")
        file = File(name="test.txt", file_path="objects/test.txt",
                    workspace_id=test_workspace.id, uploader_id=test_user.id,
                    status="pending", file_size=source.stat().st_size)
        session.add(file)
        session.commit()
        file_id = file.id
        # Separate sessions match the worker's bounded transactions.
        with patch("app.infra.indexing.handler.SessionLocal", side_effect=lambda: Session(session.get_bind())), \
             patch("app.infra.indexing.handler._download_file", return_value=str(source)), \
             patch("app.infra.indexing.handler.generate_file_description", return_value="desc"), \
             patch("app.infra.indexing.handler._embed_text", return_value=[0.1] * 1024), \
             patch("app.infra.indexing.handler._embed_texts", side_effect=lambda texts, *args, **kwargs: [[0.2] * 1024 for _ in texts]):
            handle_file_indexing(file_id)
        session.expire_all()
        saved = session.get(File, file_id)
        assert saved.status == "success"
        assert saved.description == "desc"
        assert len(saved.vector_info) == 1024
        chunks = session.query(FileChunk).filter_by(file_id=file_id).all()
        assert len(chunks) == 1
        assert "Indexable text" in chunks[0].content
        assert len(chunks[0].vector_info) == 1024

    def test_exception_marks_fail(self):
        mock_session = MagicMock()
        mock_file = MagicMock()
        mock_file.id = 1
        mock_file.name = "test.txt"
        mock_file.uploader_id = 1
        mock_file.file_path = "test.txt"
        mock_file.workspace_id = 1
        mock_storage = MagicMock()
        mock_storage.download_to_temp.return_value = "/tmp/test.txt"

        mock_session_get = MagicMock()
        mock_session_get.uploader_id = 1

        with patch("app.infra.indexing.handler.SessionLocal", return_value=mock_session), \
             patch("app.infra.indexing.handler.file_service.get_file", return_value=mock_file), \
             patch("app.infra.indexing.handler.get_vl_model_config", return_value={}), \
             patch("app.infra.indexing.handler.get_chat_model_config", return_value={}), \
             patch("app.infra.indexing.handler.get_embedding_model_config", return_value={}), \
             patch("app.infra.indexing.handler.generate_file_description", side_effect=Exception("LLM error")), \
             patch("app.infra.indexing.handler.inbox_service.create_inbox_message"):
            mock_session.get.return_value = mock_session_get
            handle_file_indexing(1)

        assert mock_session_get.status == "fail"


class TestMarkFileFailed:
    def test_success(self):
        mock_session = MagicMock()
        mock_file = MagicMock()
        mock_file.uploader_id = 1
        mock_session.get.return_value = mock_file

        with patch("app.infra.indexing.handler.inbox_service.create_inbox_message"):
            _mark_file_failed(mock_session, 1, Exception("err"))
        assert mock_file.status == "fail"

    def test_file_not_found(self):
        mock_session = MagicMock()
        mock_session.get.return_value = None
        _mark_file_failed(mock_session, 999, Exception("err"))
        mock_session.commit.assert_not_called()


class TestCompatAlias:
    def test_handle_file_process_is_handle_file_indexing(self):
        assert handle_file_process is handle_file_indexing
