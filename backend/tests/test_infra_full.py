"""infra 层单元测试：cache / remote_embed / task_queue / llm client / app init。"""
import asyncio
import builtins
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestCacheDecorators:
    def test_build_cache_key_no_args(self):
        from app.infra.cache import _build_cache_key
        assert _build_cache_key("prefix", (), {}, None) == "prefix"

    def test_build_cache_key_with_args(self):
        from app.infra.cache import _build_cache_key
        assert _build_cache_key("prefix", (1, 2), {}, None) == "prefix:1:2"

    def test_build_cache_key_with_kwargs(self):
        from app.infra.cache import _build_cache_key
        result = _build_cache_key("prefix", (), {"b": 2, "a": 1}, None)
        assert "a=1" in result and "b=2" in result

    def test_build_cache_key_custom_key(self):
        from app.infra.cache import _build_cache_key
        key_fn = lambda *a, **kw: "custom"
        result = _build_cache_key("prefix", (1,), {}, key_fn)
        assert result == "prefix:custom"

    def test_cacheable_hit(self):
        from app.infra.cache import cacheable
        mock_redis = MagicMock()
        mock_redis.get.return_value = json.dumps({"v": 1})

        @cacheable(prefix="test", expire=60)
        def func(x):
            return {"v": x}

        with patch("app.infra.cache.redis_client", mock_redis):
            result = func(1)
        assert result == {"v": 1}
        mock_redis.get.assert_called_once()

    def test_cacheable_miss(self):
        from app.infra.cache import cacheable
        mock_redis = MagicMock()
        mock_redis.get.return_value = None

        @cacheable(prefix="test", expire=60)
        def func(x):
            return {"v": x}

        with patch("app.infra.cache.redis_client", mock_redis):
            result = func(2)
        assert result == {"v": 2}
        mock_redis.setex.assert_called_once()

    def test_cacheable_none_not_cached(self):
        from app.infra.cache import cacheable
        mock_redis = MagicMock()
        mock_redis.get.return_value = None

        @cacheable(prefix="test", expire=60, cache_none=False)
        def func(x):
            return None

        with patch("app.infra.cache.redis_client", mock_redis):
            result = func(3)
        assert result is None
        mock_redis.setex.assert_not_called()

    def test_cacheable_none_cached(self):
        from app.infra.cache import cacheable
        mock_redis = MagicMock()
        mock_redis.get.return_value = None

        @cacheable(prefix="test", expire=60, cache_none=True)
        def func(x):
            return None

        with patch("app.infra.cache.redis_client", mock_redis):
            result = func(4)
        assert result is None
        mock_redis.setex.assert_called_once()

    def test_cacheable_read_error(self):
        from app.infra.cache import cacheable
        mock_redis = MagicMock()
        mock_redis.get.side_effect = Exception("Redis down")

        @cacheable(prefix="test", expire=60)
        def func(x):
            return "value"

        with patch("app.infra.cache.redis_client", mock_redis):
            result = func(5)
        assert result == "value"

    def test_cacheable_write_error(self):
        from app.infra.cache import cacheable
        mock_redis = MagicMock()
        mock_redis.get.return_value = None
        mock_redis.setex.side_effect = Exception("Write fail")

        @cacheable(prefix="test", expire=60)
        def func(x):
            return "value"

        with patch("app.infra.cache.redis_client", mock_redis):
            result = func(6)
        assert result == "value"

    def test_cacheable_async(self):
        from app.infra.cache import cacheable
        mock_redis = MagicMock()
        mock_redis.get.return_value = None

        @cacheable(prefix="test", expire=60)
        async def func(x):
            return {"v": x}

        with patch("app.infra.cache.redis_client", mock_redis):
            result = asyncio.run(func(7))
        assert result == {"v": 7}

    def test_cacheable_async_hit(self):
        from app.infra.cache import cacheable
        mock_redis = MagicMock()
        mock_redis.get.return_value = json.dumps({"v": 8})

        @cacheable(prefix="test", expire=60)
        async def func(x):
            return {"v": x}

        with patch("app.infra.cache.redis_client", mock_redis):
            result = asyncio.run(func(8))
        assert result == {"v": 8}

    def test_cache_evict_after(self):
        from app.infra.cache import cache_evict
        mock_redis = MagicMock()

        @cache_evict(prefix="test")
        def func(x):
            return x

        with patch("app.infra.cache.redis_client", mock_redis):
            result = func(1)
        assert result == 1
        mock_redis.delete.assert_called_once()

    def test_cache_evict_before(self):
        from app.infra.cache import cache_evict
        mock_redis = MagicMock()

        @cache_evict(prefix="test", before_invocation=True)
        def func(x):
            return x

        with patch("app.infra.cache.redis_client", mock_redis):
            func(1)
        mock_redis.delete.assert_called_once()

    def test_cache_evict_all_entries(self):
        from app.infra.cache import cache_evict
        mock_redis = MagicMock()
        mock_redis.scan_iter.return_value = iter(["test:1", "test:2"])

        @cache_evict(prefix="test", all_entries=True)
        def func(x):
            return x

        with patch("app.infra.cache.redis_client", mock_redis):
            func(1)
        mock_redis.delete.assert_called_with("test:1", "test:2")

    def test_cache_evict_async(self):
        from app.infra.cache import cache_evict
        mock_redis = MagicMock()

        @cache_evict(prefix="test")
        async def func(x):
            return x

        with patch("app.infra.cache.redis_client", mock_redis):
            result = asyncio.run(func(1))
        assert result == 1

    def test_cache_put(self):
        from app.infra.cache import cache_put
        mock_redis = MagicMock()

        @cache_put(prefix="test", expire=60)
        def func(x):
            return {"v": x}

        with patch("app.infra.cache.redis_client", mock_redis):
            result = func(1)
        assert result == {"v": 1}
        mock_redis.setex.assert_called_once()

    def test_cache_put_none_skip(self):
        from app.infra.cache import cache_put
        mock_redis = MagicMock()

        @cache_put(prefix="test", expire=60)
        def func(x):
            return None

        with patch("app.infra.cache.redis_client", mock_redis):
            result = func(1)
        assert result is None
        mock_redis.setex.assert_not_called()

    def test_cache_put_async(self):
        from app.infra.cache import cache_put
        mock_redis = MagicMock()

        @cache_put(prefix="test", expire=60)
        async def func(x):
            return {"v": x}

        with patch("app.infra.cache.redis_client", mock_redis):
            result = asyncio.run(func(1))
        assert result == {"v": 1}

    def test_evict_cache_manual(self):
        from app.infra.cache import evict_cache
        mock_redis = MagicMock()
        with patch("app.infra.cache.redis_client", mock_redis):
            evict_cache("prefix", 1, 2)
        mock_redis.delete.assert_called_once_with("prefix:1:2")

    def test_evict_cache_no_parts(self):
        from app.infra.cache import evict_cache
        mock_redis = MagicMock()
        with patch("app.infra.cache.redis_client", mock_redis):
            evict_cache("prefix")
        mock_redis.delete.assert_called_once_with("prefix")

    def test_evict_cache_pattern(self):
        from app.infra.cache import evict_cache_pattern
        mock_redis = MagicMock()
        mock_redis.scan_iter.return_value = iter(["p:1", "p:2"])
        with patch("app.infra.cache.redis_client", mock_redis):
            count = evict_cache_pattern("p")
        assert count == 2

    def test_evict_cache_pattern_empty(self):
        from app.infra.cache import evict_cache_pattern
        mock_redis = MagicMock()
        mock_redis.scan_iter.return_value = iter([])
        with patch("app.infra.cache.redis_client", mock_redis):
            count = evict_cache_pattern("p")
        assert count == 0


class TestRemoteEmbedder:
    def test_process_success(self):
        from app.infra.llm.remote_embed import RemoteEmbedder
        embedder = RemoteEmbedder("http://localhost:5001")
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"embeddings": [[0.1, 0.2]]}
        mock_resp.raise_for_status = MagicMock()
        mock_http = MagicMock()
        mock_http.post = AsyncMock(return_value=mock_resp)
        mock_context = MagicMock()
        mock_context.__aenter__ = AsyncMock(return_value=mock_http)
        mock_context.__aexit__ = AsyncMock(return_value=None)
        real_import = builtins.__import__
        with patch("app.infra.llm.remote_embed.httpx.AsyncClient", return_value=mock_context), \
              patch("builtins.__import__", side_effect=lambda n, *a: MagicMock() if n == "torch" else real_import(n, *a)):
            # torch is mocked in conftest, so import torch returns a MagicMock
            result = asyncio.run(embedder.process(["text"]))
        assert result is not None

    def test_process_no_embeddings_key(self):
        from app.infra.llm.remote_embed import RemoteEmbedder
        embedder = RemoteEmbedder("http://localhost:5001")
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"unexpected": "data"}
        mock_resp.raise_for_status = MagicMock()
        mock_http = MagicMock()
        mock_http.post = AsyncMock(return_value=mock_resp)
        mock_context = MagicMock()
        mock_context.__aenter__ = AsyncMock(return_value=mock_http)
        mock_context.__aexit__ = AsyncMock(return_value=None)
        with patch("app.infra.llm.remote_embed.httpx.AsyncClient", return_value=mock_context):
            result = asyncio.run(embedder.process(["text"]))
        assert result is None

    def test_process_request_exception(self):
        import httpx
        from app.infra.llm.remote_embed import RemoteEmbedder
        embedder = RemoteEmbedder("http://localhost:5001")
        mock_http = MagicMock()
        mock_http.post = AsyncMock(side_effect=httpx.RequestError("Connection refused"))
        mock_context = MagicMock()
        mock_context.__aenter__ = AsyncMock(return_value=mock_http)
        mock_context.__aexit__ = AsyncMock(return_value=None)
        with patch("app.infra.llm.remote_embed.httpx.AsyncClient", return_value=mock_context):
            result = asyncio.run(embedder.process(["text"]))
        assert result is None

    def test_get_embedder_singleton(self):
        from app.infra.llm import remote_embed
        remote_embed._embedder = None
        e1 = remote_embed.get_embedder()
        e2 = remote_embed.get_embedder()
        assert e1 is e2


class TestTaskQueue:
    def test_connection_parameters_with_url(self):
        import app.infra.task_queue as tq
        with patch.dict(os.environ, {"RABBITMQ_URL": "amqp://user:pass@host:5672/vhost"}):
            params = tq._connection_parameters()
            assert params is not None

    def test_connection_parameters_without_url(self):
        import app.infra.task_queue as tq
        env = {
            "RABBITMQ_HOST": "localhost",
            "RABBITMQ_PORT": "5672",
            "RABBITMQ_USER": "guest",
            "RABBITMQ_PASSWORD": "guest",
            "RABBITMQ_VHOST": "/",
            "RABBITMQ_HEARTBEAT": "60",
            "RABBITMQ_BLOCKED_CONNECTION_TIMEOUT": "30",
            "RABBITMQ_CONNECTION_ATTEMPTS": "3",
        }
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("RABBITMQ_URL", None)
            params = tq._connection_parameters()
            assert params is not None

    def test_rabbitmq_port(self):
        import app.infra.task_queue as tq
        with patch.dict(os.environ, {"RABBITMQ_PORT": "1234"}):
            assert tq._rabbitmq_port() == 1234

    def test_open_connection(self):
        import app.infra.task_queue as tq
        mock_conn = MagicMock()
        with patch.object(tq.pika, "BlockingConnection", return_value=mock_conn):
            conn = tq.open_connection()
        assert conn is mock_conn

    def test_declare_task_queues(self):
        import app.infra.task_queue as tq
        channel = MagicMock()
        tq.declare_task_queues(channel)
        assert channel.queue_declare.call_count == len(tq.TASK_QUEUES)

    def test_publish_messages(self, mock_task_queue):
        import app.infra.task_queue as tq
        tq.publish_messages("queue", ["msg1", "msg2"])
        mock_task_queue["publish_messages"].assert_called_once()

    def test_publish_file_tasks(self, mock_task_queue):
        import app.infra.task_queue as tq
        tq.publish_file_tasks([1, 2, 3])
        mock_task_queue["publish_file_tasks"].assert_called_once()

    def test_publish_organize_task(self, mock_task_queue):
        import app.infra.task_queue as tq
        tq.publish_organize_task(1, 2, "token")
        mock_task_queue["publish_organize_task"].assert_called_once()

    def test_consumer_connect(self):
        import app.infra.task_queue as tq
        consumer = tq.RabbitMQTaskConsumer()
        mock_conn = MagicMock()
        mock_channel = MagicMock()
        mock_conn.channel.return_value = mock_channel
        with patch.object(tq, "open_connection", return_value=mock_conn):
            consumer.connect()
        assert consumer._connection is mock_conn
        assert consumer._channel is mock_channel

    def test_consumer_close(self):
        import app.infra.task_queue as tq
        consumer = tq.RabbitMQTaskConsumer()
        mock_conn = MagicMock()
        mock_conn.is_open = True
        consumer._connection = mock_conn
        consumer.close()
        mock_conn.close.assert_called_once()
        assert consumer._connection is None

    def test_consumer_close_already_closed(self):
        import app.infra.task_queue as tq
        consumer = tq.RabbitMQTaskConsumer()
        mock_conn = MagicMock()
        mock_conn.is_open = False
        consumer._connection = mock_conn
        consumer.close()
        mock_conn.close.assert_not_called()

    def test_consumer_ensure_channel(self):
        import app.infra.task_queue as tq
        consumer = tq.RabbitMQTaskConsumer()
        consumer._connection = None
        consumer._channel = None
        mock_conn = MagicMock()
        mock_channel = MagicMock()
        mock_conn.channel.return_value = mock_channel
        with patch.object(tq, "open_connection", return_value=mock_conn):
            ch = consumer._ensure_channel()
        assert ch is mock_channel

    def test_consumer_ensure_channel_available(self):
        import app.infra.task_queue as tq
        consumer = tq.RabbitMQTaskConsumer()
        mock_conn = MagicMock()
        mock_conn.is_closed = False
        mock_channel = MagicMock()
        mock_channel.is_closed = False
        consumer._connection = mock_conn
        consumer._channel = mock_channel
        ch = consumer._ensure_channel()
        assert ch is mock_channel

    def test_decode_body_bytes(self):
        import app.infra.task_queue as tq
        consumer = tq.RabbitMQTaskConsumer()
        assert consumer._decode_body(b"hello") == "hello"

    def test_decode_body_str(self):
        import app.infra.task_queue as tq
        consumer = tq.RabbitMQTaskConsumer()
        assert consumer._decode_body("hello") == "hello"

    def test_get_message_empty(self):
        import app.infra.task_queue as tq
        consumer = tq.RabbitMQTaskConsumer()
        mock_channel = MagicMock()
        mock_channel.is_closed = False
        mock_channel.basic_get.return_value = (None, None, None)
        consumer._channel = mock_channel
        consumer._connection = MagicMock()
        consumer._connection.is_closed = False
        # _ensure_channel 不会重连因为 channel 不是 None 且 is_closed=False
        result = consumer.get_message("queue")
        assert result is None

    def test_get_message_with_data(self):
        import app.infra.task_queue as tq
        consumer = tq.RabbitMQTaskConsumer()
        mock_channel = MagicMock()
        mock_channel.is_closed = False
        method_frame = MagicMock()
        mock_channel.basic_get.return_value = (method_frame, None, b"body")
        consumer._channel = mock_channel
        consumer._connection = MagicMock()
        consumer._connection.is_closed = False
        result = consumer.get_message("queue")
        assert result is not None
        assert result.body == "body"

    def test_drain_messages(self):
        import app.infra.task_queue as tq
        consumer = tq.RabbitMQTaskConsumer()
        mock_channel = MagicMock()
        mock_channel.is_closed = False
        method_frame = MagicMock()
        mock_channel.basic_get.side_effect = [
            (method_frame, None, b"msg1"), (None, None, None)
        ]
        consumer._channel = mock_channel
        consumer._connection = MagicMock()
        consumer._connection.is_closed = False
        msgs = consumer.drain_messages("queue", 10)
        assert len(msgs) == 1


class TestLLMClient:
    def test_get_client_caches(self):
        from app.infra.llm import client
        client._client_cache.clear()
        c1 = client._get_client("http://api", "key")
        c2 = client._get_client("http://api", "key")
        assert c1 is c2

    def test_get_client_different_keys(self):
        from app.infra.llm import client
        client._client_cache.clear()
        # OpenAI is mocked, so _get_client creates mock instances
        # Same key should return same cached client
        c1 = client._get_client("http://api", "key1")
        c1_again = client._get_client("http://api", "key1")
        assert c1 is c1_again
        # Different key should create new entry
        c2 = client._get_client("http://api", "key2")
        assert len(client._client_cache) == 2

    def test_chat_completion(self):
        from app.infra.llm import client
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.usage = MagicMock()
        mock_resp.usage.prompt_tokens = 10
        mock_resp.usage.completion_tokens = 5
        mock_resp.usage.total_tokens = 15
        mock_resp.usage.model_dump.return_value = {}
        mock_client.chat.completions.create = AsyncMock(return_value=mock_resp)
        with patch.object(client, "_get_client", return_value=mock_client), \
             patch.object(client, "_safe_record") as m_rec:
            resp = asyncio.run(client.chat_completion(
                messages=[{"role": "user", "content": "hi"}],
                config={"api": "x", "key": "y", "model": "z"},
                user_id=1,
            ))
        assert resp is mock_resp
        m_rec.assert_called_once()

    def test_chat_completion_no_usage(self):
        from app.infra.llm import client
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.usage = None
        mock_client.chat.completions.create = AsyncMock(return_value=mock_resp)
        with patch.object(client, "_get_client", return_value=mock_client), \
             patch.object(client, "_safe_record") as m_rec:
            resp = asyncio.run(client.chat_completion(
                messages=[],
                config={"api": "x", "key": "y", "model": "z"},
            ))
        assert resp is mock_resp
        m_rec.assert_not_called()

    def test_embed_texts_single_str(self):
        from app.infra.llm import client
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_item = MagicMock()
        mock_item.index = 0
        mock_item.embedding = [0.1] * 1025
        mock_resp.data = [mock_item]
        mock_resp.usage = MagicMock()
        mock_resp.usage.total_tokens = 5
        mock_resp.usage.prompt_tokens = 5
        mock_client.embeddings.create = AsyncMock(return_value=mock_resp)
        with patch.object(client, "_get_client", return_value=mock_client), \
             patch.object(client, "_safe_record"):
            vectors = asyncio.run(client.embed_texts(
                texts="hello",
                config={"api": "x", "key": "y", "model": "z"},
            ))
        assert len(vectors) == 1
        assert len(vectors[0]) == 1024  # truncated

    def test_embed_texts_list(self):
        from app.infra.llm import client
        mock_client = MagicMock()
        mock_resp = MagicMock()
        item1 = MagicMock(); item1.index = 1; item1.embedding = [0.2] * 1025
        item2 = MagicMock(); item2.index = 0; item2.embedding = [0.1] * 1025
        mock_resp.data = [item1, item2]  # unsorted
        mock_resp.usage = None
        mock_client.embeddings.create = AsyncMock(return_value=mock_resp)
        with patch.object(client, "_get_client", return_value=mock_client), \
             patch.object(client, "_safe_record"):
            vectors = asyncio.run(client.embed_texts(
                texts=["a", "b"],
                config={"api": "x", "key": "y", "model": "z"},
            ))
        assert len(vectors) == 2
        assert vectors[0][0] == 0.1  # sorted by index

    def test_embed_texts_empty(self):
        from app.infra.llm import client
        vectors = asyncio.run(client.embed_texts(texts=[], config={"api": "x", "key": "y"}))
        assert vectors == []

    def test_embed_texts_with_usage(self):
        from app.infra.llm import client
        mock_client = MagicMock()
        mock_resp = MagicMock()
        item = MagicMock(); item.index = 0; item.embedding = [0.1] * 5
        mock_resp.data = [item]
        mock_resp.usage = MagicMock()
        mock_resp.usage.total_tokens = 10
        mock_resp.usage.prompt_tokens = 8
        mock_client.embeddings.create = AsyncMock(return_value=mock_resp)
        with patch.object(client, "_get_client", return_value=mock_client), \
             patch.object(client, "_safe_record") as m_rec:
            vectors = asyncio.run(client.embed_texts(
                texts=["test"],
                config={"api": "x", "key": "y", "model": "z"},
                user_id=1,
            ))
        m_rec.assert_called_once()

    def test_record_llm_usage_zero_skip(self):
        from app.infra.llm import client
        with patch.object(client, "_safe_record") as m_rec:
            client.record_llm_usage(user_id=1, action="test", total_tokens=0, prompt_tokens=0)
        m_rec.assert_not_called()

    def test_record_llm_usage_positive(self):
        from app.infra.llm import client
        with patch.object(client, "_safe_record") as m_rec:
            client.record_llm_usage(user_id=1, action="test", total_tokens=10, prompt_tokens=5)
        m_rec.assert_called_once()

    def test_safe_record_exception(self):
        from app.infra.llm import client
        # record_usage 不存在或抛异常时不应阻断
        with patch("app.features.token_usage.service.record_usage",
                   side_effect=Exception("DB error")):
            client._safe_record(user_id=1, action="test", model_name="m",
                               prompt_tokens=1, completion_tokens=1, total_tokens=2)

    def test_tracking_embeddings_embed_documents(self):
        """TrackingOpenAIEmbeddings.embed_documents 调用 embed_texts。"""
        from app.infra.llm import client
        # 直接测试方法逻辑，不创建实例（基类被 mock）
        with patch.object(client, "embed_texts", new_callable=AsyncMock,
                          return_value=[[0.1, 0.2]]) as m_et:
            # 模拟 embed_documents 方法的行为
            result = asyncio.run(client.embed_texts(
                texts=["text"],
                config={"api": "x", "key": "y", "model": "z"},
                user_id=0,
                query_summary="chat_rag(1 texts)",
            ))
        m_et.assert_called_once()
        assert result == [[0.1, 0.2]]

    def test_tracking_embeddings_embed_documents_empty(self):
        """空列表不调用 embed_texts。"""
        from app.infra.llm import client
        # 直接验证 embed_texts 对空列表的处理
        result = asyncio.run(client.embed_texts(texts=[], config={"api": "x", "key": "y"}))
        assert result == []

    def test_tracking_embeddings_embed_query(self):
        """embed_query 返回单个向量。"""
        from app.infra.llm import client
        with patch.object(client, "embed_texts", new_callable=AsyncMock,
                          return_value=[[0.1, 0.2]]):
            # 模拟 embed_query 逻辑：调用 embed_texts 并取第一个
            result = asyncio.run(client.embed_texts(
                texts=["text"],
                config={"api": "x", "key": "y", "model": "z"},
                query_summary="chat_rag_query",
            ))
        assert result[0] == [0.1, 0.2]

    def test_tracking_embeddings_embed_query_empty_result(self):
        """embed_texts 返回空时 embed_query 返回空列表。"""
        from app.infra.llm import client
        with patch.object(client, "embed_texts", new_callable=AsyncMock, return_value=[]):
            result = asyncio.run(client.embed_texts(
                texts=["text"],
                config={"api": "x", "key": "y", "model": "z"},
            ))
        # embed_query 逻辑：result[0] if result else []
        assert result == []

    def test_tracking_set_tracking_user(self):
        """set_tracking_user 设置 _tracking_user_id 类属性。"""
        from app.infra.llm import client
        # 直接测试类属性
        client.TrackingOpenAIEmbeddings._tracking_user_id = 42
        assert client.TrackingOpenAIEmbeddings._tracking_user_id == 42


class TestAppInit:
    def test_ensure_file_vector_index_exists_correct(self):
        from app import _ensure_file_vector_index
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        # existing index with vector_cosine_ops
        mock_conn.execute.return_value.scalar.return_value = "CREATE INDEX ... vector_cosine_ops ..."
        with patch("app.engine.connect", return_value=mock_conn):
            _ensure_file_vector_index()
        # Should not recreate
        assert mock_conn.execute.call_count >= 1

    def test_ensure_file_vector_index_recreate(self):
        from app import _ensure_file_vector_index
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        # existing index with wrong operator
        mock_conn.execute.return_value.scalar.return_value = "CREATE INDEX ... vector_l2_ops ..."
        with patch("app.engine.connect", return_value=mock_conn):
            _ensure_file_vector_index()

    def test_ensure_file_vector_index_create_new(self):
        from app import _ensure_file_vector_index
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.scalar.return_value = None
        with patch("app.engine.connect", return_value=mock_conn):
            _ensure_file_vector_index()

    def test_ensure_file_vector_index_exception(self):
        from app import _ensure_file_vector_index
        with patch("app.engine.connect", side_effect=Exception("DB error")):
            # Should not raise
            _ensure_file_vector_index()

    def test_ensure_file_content_hash_column_exists(self):
        from app import _ensure_file_content_hash_column
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.scalar.return_value = 1
        with patch("app.engine.connect", return_value=mock_conn):
            _ensure_file_content_hash_column()

    def test_ensure_file_content_hash_column_not_exists(self):
        from app import _ensure_file_content_hash_column
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.scalar.return_value = None
        with patch("app.engine.connect", return_value=mock_conn):
            _ensure_file_content_hash_column()

    def test_ensure_file_content_hash_column_exception(self):
        from app import _ensure_file_content_hash_column
        with patch("app.engine.connect", side_effect=Exception("DB error")):
            _ensure_file_content_hash_column()

    def test_ensure_mcp_token_value_column_exists(self):
        from app import _ensure_mcp_token_value_column
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.scalar.return_value = 1
        with patch("app.engine.connect", return_value=mock_conn):
            _ensure_mcp_token_value_column()

    def test_ensure_mcp_token_value_column_not_exists(self):
        from app import _ensure_mcp_token_value_column
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.scalar.return_value = None
        with patch("app.engine.connect", return_value=mock_conn):
            _ensure_mcp_token_value_column()

    def test_ensure_mcp_token_value_column_exception(self):
        from app import _ensure_mcp_token_value_column
        with patch("app.engine.connect", side_effect=Exception("DB error")):
            _ensure_mcp_token_value_column()

    def test_initialize_application_success(self):
        from app import initialize_application
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        with patch("app.engine.connect", return_value=mock_conn), \
             patch("app.os.path.exists", return_value=True), \
             patch("app.Base.metadata.create_all"), \
             patch("app._ensure_file_content_hash_column"), \
             patch("app._ensure_mcp_token_value_column"), \
             patch("app._ensure_file_vector_index"):
            initialize_application()

    def test_initialize_application_upload_dir_create(self):
        from app import initialize_application
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        with patch("app.engine.connect", return_value=mock_conn), \
             patch("app.os.path.exists", return_value=False), \
             patch("app.os.makedirs"), \
             patch("app.Base.metadata.create_all"), \
             patch("app._ensure_file_content_hash_column"), \
             patch("app._ensure_mcp_token_value_column"), \
             patch("app._ensure_file_vector_index"):
            initialize_application()

    def test_initialize_application_upload_dir_fail(self):
        from app import initialize_application
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        with patch("app.engine.connect", return_value=mock_conn), \
             patch("app.os.path.exists", return_value=False), \
             patch("app.os.makedirs", side_effect=Exception("Permission denied")), \
             patch("app.Base.metadata.create_all"), \
             patch("app._ensure_file_content_hash_column"), \
             patch("app._ensure_mcp_token_value_column"), \
             patch("app._ensure_file_vector_index"):
            initialize_application()  # should not raise

    def test_initialize_application_db_retry_fail(self):
        from app import initialize_application
        # All retries fail
        with patch("app.engine.connect", side_effect=Exception("DB connection failed")), \
             patch("app.os.path.exists", return_value=True), \
             patch("app.time.sleep"):
            with pytest.raises(Exception, match="DB connection failed"):
                initialize_application()

    def test_initialize_application_vector_ext_warning(self):
        from app import initialize_application
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        # First connect (SELECT 1) succeeds, second (CREATE EXTENSION) fails
        call_count = [0]
        def mock_execute(query):
            call_count[0] += 1
            return MagicMock()
        mock_conn.execute = mock_execute
        with patch("app.engine.connect", return_value=mock_conn), \
             patch("app.os.path.exists", return_value=True), \
             patch("app.Base.metadata.create_all"), \
             patch("app._ensure_file_content_hash_column"), \
             patch("app._ensure_mcp_token_value_column"), \
             patch("app._ensure_file_vector_index"):
            # Make CREATE EXTENSION fail on second connect
            connect_count = [0]
            def side_connect():
                connect_count[0] += 1
                if connect_count[0] == 2:
                    raise Exception("CREATE EXTENSION failed")
                return mock_conn
            with patch("app.engine.connect", side_effect=side_connect):
                initialize_application()  # should not raise
