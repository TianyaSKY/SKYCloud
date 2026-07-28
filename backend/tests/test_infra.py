"""基础设施工具测试：datetime_utils / cache / upload_adapter / llm config。"""

import base64
import json
import os
import tempfile
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.infra.datetime_utils import beijing_now, to_beijing_naive, local_isoformat, BEIJING_TZ


# ---------------------------------------------------------------------------
# datetime_utils
# ---------------------------------------------------------------------------


class TestBeijingNow:
    def test_returns_naive_datetime(self):
        now = beijing_now()
        assert isinstance(now, datetime)
        assert now.tzinfo is None

    def test_close_to_actual_time(self):
        now = beijing_now()
        expected = datetime.now(BEIJING_TZ).replace(tzinfo=None)
        assert abs((now - expected).total_seconds()) < 2


class TestToBeijingNaive:
    def test_none_returns_none(self):
        assert to_beijing_naive(None) is None

    def test_naive_returns_same(self):
        dt = datetime(2025, 6, 1, 12, 0, 0)
        assert to_beijing_naive(dt) is dt

    def test_utc_aware_converts_to_beijing(self):
        utc_dt = datetime(2025, 6, 1, 4, 0, 0, tzinfo=timezone.utc)
        result = to_beijing_naive(utc_dt)
        assert result.tzinfo is None
        assert result.hour == 12  # UTC+8

    def test_other_timezone_converts(self):
        us_eastern = ZoneInfo("America/New_York")
        dt = datetime(2025, 6, 1, 0, 0, 0, tzinfo=us_eastern)
        result = to_beijing_naive(dt)
        assert result.tzinfo is None
        # 纽约 00:00 EDT = UTC 04:00 = 北京 12:00
        assert result.hour == 12


class TestLocalIsoformat:
    def test_none_returns_none(self):
        assert local_isoformat(None) is None

    def test_naive_datetime(self):
        dt = datetime(2025, 3, 15, 10, 30, 0)
        result = local_isoformat(dt)
        assert result == "2025-03-15T10:30:00"

    def test_aware_datetime_converts(self):
        utc_dt = datetime(2025, 6, 1, 4, 0, 0, tzinfo=timezone.utc)
        result = local_isoformat(utc_dt)
        assert "12:00:00" in result


# ---------------------------------------------------------------------------
# cache 装饰器
# ---------------------------------------------------------------------------


class TestCacheBuildKey:
    def test_build_key_with_key_func(self):
        from app.infra.cache import _build_cache_key

        key_func = lambda session, id, **_: id
        result = _build_cache_key("user:profile", (None, 42), {}, key_func)
        assert result == "user:profile:42"

    def test_build_key_without_key_func(self):
        from app.infra.cache import _build_cache_key

        result = _build_cache_key("prefix", ("a", "b"), {})
        assert result == "prefix:a:b"

    def test_build_key_with_kwargs(self):
        from app.infra.cache import _build_cache_key

        result = _build_cache_key("prefix", (), {"x": 1, "y": 2})
        assert "x=1" in result
        assert "y=2" in result

    def test_build_key_empty(self):
        from app.infra.cache import _build_cache_key

        result = _build_cache_key("prefix", (), {})
        assert result == "prefix"


class TestCacheable:
    def test_cache_hit(self, mock_redis):
        from app.infra.cache import cacheable

        mock_redis.get.return_value = json.dumps({"data": "cached"})

        @cacheable(prefix="test", expire=60)
        def my_func(x):
            return {"data": "fresh"}

        result = my_func(1)
        assert result == {"data": "cached"}

    def test_cache_miss(self, mock_redis):
        from app.infra.cache import cacheable

        mock_redis.get.return_value = None

        @cacheable(prefix="test", expire=60)
        def my_func(x):
            return {"data": "fresh"}

        result = my_func(1)
        assert result == {"data": "fresh"}
        mock_redis.setex.assert_called_once()

    def test_none_not_cached_by_default(self, mock_redis):
        from app.infra.cache import cacheable

        mock_redis.get.return_value = None

        @cacheable(prefix="test", expire=60)
        def my_func(x):
            return None

        result = my_func(1)
        assert result is None
        mock_redis.setex.assert_not_called()

    def test_none_cached_when_cache_none_true(self, mock_redis):
        from app.infra.cache import cacheable

        mock_redis.get.return_value = None

        @cacheable(prefix="test", expire=60, cache_none=True)
        def my_func(x):
            return None

        result = my_func(1)
        assert result is None
        mock_redis.setex.assert_called_once()

    def test_redis_read_error_fallback(self, mock_redis):
        from app.infra.cache import cacheable

        mock_redis.get.side_effect = Exception("Redis down")

        @cacheable(prefix="test", expire=60)
        def my_func(x):
            return "fallback"

        result = my_func(1)
        assert result == "fallback"


class TestCacheEvict:
    def test_evict_after_invocation(self, mock_redis):
        from app.infra.cache import cache_evict

        @cache_evict(prefix="test")
        def my_func(x):
            return "done"

        result = my_func(1)
        assert result == "done"
        mock_redis.delete.assert_called_once()

    def test_evict_before_invocation(self, mock_redis):
        from app.infra.cache import cache_evict

        call_order = []

        @cache_evict(prefix="test", before_invocation=True)
        def my_func(x):
            call_order.append("func")
            return "done"

        my_func(1)
        # delete 在 func 之前调用
        assert mock_redis.delete.called

    def test_evict_all_entries(self, mock_redis):
        from app.infra.cache import cache_evict

        mock_redis.scan_iter.return_value = iter(["test:1", "test:2"])

        @cache_evict(prefix="test", all_entries=True)
        def my_func():
            return "done"

        my_func()
        mock_redis.delete.assert_called_once_with("test:1", "test:2")


class TestCachePut:
    def test_always_writes(self, mock_redis):
        from app.infra.cache import cache_put

        @cache_put(prefix="test", expire=120)
        def my_func(x):
            return {"value": x}

        result = my_func(42)
        assert result == {"value": 42}
        mock_redis.setex.assert_called_once()

    def test_none_not_written_by_default(self, mock_redis):
        from app.infra.cache import cache_put

        @cache_put(prefix="test")
        def my_func(x):
            return None

        my_func(1)
        mock_redis.setex.assert_not_called()


class TestEvictCacheManual:
    def test_evict_cache_with_parts(self, mock_redis):
        from app.infra.cache import evict_cache

        evict_cache("user:profile", 42)
        mock_redis.delete.assert_called_once_with("user:profile:42")

    def test_evict_cache_no_parts(self, mock_redis):
        from app.infra.cache import evict_cache

        evict_cache("sys_dict:all")
        mock_redis.delete.assert_called_once_with("sys_dict:all")

    def test_evict_cache_pattern(self, mock_redis):
        from app.infra.cache import evict_cache_pattern

        mock_redis.scan_iter.return_value = iter(["prefix:1", "prefix:2"])
        count = evict_cache_pattern("prefix")
        assert count == 2
        mock_redis.delete.assert_called_once_with("prefix:1", "prefix:2")


# ---------------------------------------------------------------------------
# upload_adapter
# ---------------------------------------------------------------------------


class TestFastAPIUploadAdapter:
    def test_save(self, tmp_path):
        from io import BytesIO
        from fastapi import UploadFile
        from app.infra.upload_adapter import FastAPIUploadAdapter

        content = b"hello world"
        file_obj = UploadFile(
            filename="test.txt",
            file=BytesIO(content),
            headers={"content-type": "text/plain"},
        )
        adapter = FastAPIUploadAdapter(file_obj)
        assert adapter.filename == "test.txt"
        assert adapter.mimetype == "text/plain"

        dest = str(tmp_path / "output.txt")
        adapter.save(dest)
        with open(dest, "rb") as f:
            assert f.read() == content


class TestBase64UploadAdapter:
    def test_data_uri_png(self, tmp_path):
        from app.infra.upload_adapter import Base64UploadAdapter

        raw = b"\x89PNG\r\n"
        b64 = base64.b64encode(raw).decode()
        data_uri = f"data:image/png;base64,{b64}"

        adapter = Base64UploadAdapter(data_uri)
        assert adapter.mimetype == "image/png"
        assert adapter.filename.endswith(".png")

        dest = str(tmp_path / "out.png")
        adapter.save(dest)
        with open(dest, "rb") as f:
            assert f.read() == raw

    def test_bare_base64(self, tmp_path):
        from app.infra.upload_adapter import Base64UploadAdapter

        raw = b"binary data"
        b64 = base64.b64encode(raw).decode()

        adapter = Base64UploadAdapter(b64)
        assert adapter.mimetype == "application/octet-stream"

        dest = str(tmp_path / "out.bin")
        adapter.save(dest)
        with open(dest, "rb") as f:
            assert f.read() == raw

    def test_custom_filename(self):
        from app.infra.upload_adapter import Base64UploadAdapter

        b64 = base64.b64encode(b"data").decode()
        adapter = Base64UploadAdapter(b64, filename="custom.jpg")
        assert adapter.filename == "custom.jpg"

    def test_filename_without_extension(self):
        from app.infra.upload_adapter import Base64UploadAdapter

        b64 = base64.b64encode(b"data").decode()
        data_uri = f"data:image/jpeg;base64,{b64}"
        adapter = Base64UploadAdapter(data_uri, filename="photo")
        assert "." in adapter.filename  # 应自动补扩展名

    def test_content_type_property(self):
        from app.infra.upload_adapter import Base64UploadAdapter

        b64 = base64.b64encode(b"x").decode()
        adapter = Base64UploadAdapter(f"data:image/gif;base64,{b64}")
        assert adapter.content_type == "image/gif"


# ---------------------------------------------------------------------------
# LLM config
# ---------------------------------------------------------------------------


class TestLLMConfig:
    def test_is_model_config_sys_dict_key(self):
        from app.infra.llm.config import is_model_config_sys_dict_key

        assert is_model_config_sys_dict_key("chat_api_url") is True
        assert is_model_config_sys_dict_key("CHAT_API_KEY") is True
        assert is_model_config_sys_dict_key("emb_model_name") is True
        assert is_model_config_sys_dict_key("random_key") is False
        assert is_model_config_sys_dict_key(None) is False
        assert is_model_config_sys_dict_key("") is False

    def test_get_chat_model_config_defaults(self):
        from app.infra.llm.config import get_chat_model_config

        config = get_chat_model_config()
        assert "api" in config
        assert "key" in config
        assert "model" in config

    def test_get_embedding_model_config(self):
        from app.infra.llm.config import get_embedding_model_config

        config = get_embedding_model_config()
        assert "api" in config
        assert "model" in config

    def test_get_vl_model_config(self):
        from app.infra.llm.config import get_vl_model_config

        config = get_vl_model_config()
        assert "api" in config
        assert "model" in config

    def test_get_rerank_model_config(self):
        from app.infra.llm.config import get_rerank_model_config

        config = get_rerank_model_config()
        assert "api" in config
        assert "model" in config

    def test_get_rerank_top_k_default(self):
        from app.infra.llm.config import get_rerank_top_k

        top_k = get_rerank_top_k()
        assert isinstance(top_k, int)
        assert top_k > 0

    def test_read_env_with_override(self, monkeypatch):
        from app.infra.llm.config import get_chat_model_config

        monkeypatch.setenv("CHAT_API_URL", "http://custom:8080/v1")
        monkeypatch.setenv("CHAT_API_MODEL", "custom-model")
        config = get_chat_model_config()
        assert config["api"] == "http://custom:8080/v1"
        assert config["model"] == "custom-model"
