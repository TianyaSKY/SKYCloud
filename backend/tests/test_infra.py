"""基础设施工具测试：datetime_utils / cache / upload_adapter / llm config。"""

import base64
from datetime import datetime, timezone
from io import BytesIO
import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import UploadFile
import pytest
from starlette.datastructures import Headers

from app.infra.cache import CacheableParams
from app.infra.datetime_utils import beijing_now, to_beijing_naive, local_isoformat, BEIJING_TZ


# datetime_utils


class TestBeijingNow:
    def test_returns_naive_datetime(self) -> None:
        now = beijing_now()
        assert isinstance(now, datetime)
        assert now.tzinfo is None

    def test_close_to_actual_time(self) -> None:
        now = beijing_now()
        expected = datetime.now(BEIJING_TZ).replace(tzinfo=None)
        assert abs((now - expected).total_seconds()) < 2


class TestToBeijingNaive:
    def test_none_returns_none(self) -> None:
        assert to_beijing_naive(None) is None

    def test_naive_returns_same(self) -> None:
        dt = datetime(2025, 6, 1, 12, 0, 0)
        assert to_beijing_naive(dt) is dt

    def test_utc_aware_converts_to_beijing(self) -> None:
        utc_dt = datetime(2025, 6, 1, 4, 0, 0, tzinfo=timezone.utc)
        result = to_beijing_naive(utc_dt)
        assert result is not None
        assert result.tzinfo is None
        assert result.hour == 12  # UTC+8

    def test_other_timezone_converts(self) -> None:
        us_eastern = ZoneInfo("America/New_York")
        dt = datetime(2025, 6, 1, 0, 0, 0, tzinfo=us_eastern)
        result = to_beijing_naive(dt)
        assert result is not None
        assert result.tzinfo is None
        # 纽约 00:00 EDT = UTC 04:00 = 北京 12:00
        assert result.hour == 12


class TestLocalIsoformat:
    def test_none_returns_none(self) -> None:
        assert local_isoformat(None) is None

    def test_naive_datetime(self) -> None:
        dt = datetime(2025, 3, 15, 10, 30, 0)
        result = local_isoformat(dt)
        assert result == "2025-03-15T02:30:00+00:00"

    def test_aware_datetime_converts(self) -> None:
        utc_dt = datetime(2025, 6, 1, 4, 0, 0, tzinfo=timezone.utc)
        result = local_isoformat(utc_dt)
        assert result == "2025-06-01T04:00:00+00:00"


# cache 装饰器


class TestCacheBuildKey:
    def test_build_key_with_key_func(self) -> None:
        from app.infra.cache import _build_cache_key  # pyright: ignore[reportPrivateUsage]

        def key_func(session: Any, id: int, **_: Any) -> int:
            return id
        result = _build_cache_key("user:profile", (None, 42), {}, key_func)
        assert result == "user:profile:42"

    def test_build_key_without_key_func(self) -> None:
        from app.infra.cache import _build_cache_key  # pyright: ignore[reportPrivateUsage]

        result = _build_cache_key("prefix", ("a", "b"), {})
        assert result == "prefix:a:b"

    def test_build_key_with_kwargs(self) -> None:
        from app.infra.cache import _build_cache_key  # pyright: ignore[reportPrivateUsage]

        result = _build_cache_key("prefix", (), {"x": 1, "y": 2})
        assert "x=1" in result
        assert "y=2" in result

    def test_build_key_empty(self) -> None:
        from app.infra.cache import _build_cache_key  # pyright: ignore[reportPrivateUsage]

        result = _build_cache_key("prefix", (), {})
        assert result == "prefix"


class TestCacheable:
    def test_cache_hit(self, mock_redis: Any) -> None:
        from app.infra.cache import cacheable

        mock_redis.get.return_value = json.dumps({"data": "cached"})

        @cacheable(CacheableParams(prefix="test", expire=60))
        def my_func(x: int) -> dict[str, str]:
            return {"data": "fresh"}

        result = my_func(1)
        assert result == {"data": "cached"}

    def test_cache_miss(self, mock_redis: Any) -> None:
        from app.infra.cache import cacheable

        mock_redis.get.return_value = None

        @cacheable(CacheableParams(prefix="test", expire=60))
        def my_func(x: int) -> dict[str, str]:
            return {"data": "fresh"}

        result = my_func(1)
        assert result == {"data": "fresh"}
        mock_redis.setex.assert_called_once()

    def test_none_not_cached_by_default(self, mock_redis: Any) -> None:
        from app.infra.cache import cacheable

        mock_redis.get.return_value = None

        @cacheable(CacheableParams(prefix="test", expire=60))
        def my_func(x: int) -> None:
            return None

        result = my_func(1)
        assert result is None
        mock_redis.setex.assert_not_called()

    def test_none_cached_when_cache_none_true(self, mock_redis: Any) -> None:
        from app.infra.cache import cacheable

        mock_redis.get.return_value = None

        @cacheable(CacheableParams(prefix="test", expire=60, cache_none=True))
        def my_func(x: int) -> None:
            return None

        result = my_func(1)
        assert result is None
        mock_redis.setex.assert_called_once()

    def test_redis_read_error_fallback(self, mock_redis: Any) -> None:
        from app.infra.cache import cacheable

        mock_redis.get.side_effect = Exception("Redis down")

        @cacheable(CacheableParams(prefix="test", expire=60))
        def my_func(x: int) -> str:
            return "fallback"

        result = my_func(1)
        assert result == "fallback"


class TestEvictCachePattern:
    def test_evict_cache_pattern(self, mock_redis: Any) -> None:
        from app.infra.cache import evict_cache_pattern

        mock_redis.scan_iter.return_value = iter(["prefix:1", "prefix:2"])
        count = evict_cache_pattern("prefix")
        assert count == 2
        mock_redis.delete.assert_called_once_with("prefix:1", "prefix:2")


# upload_adapter


class TestFastAPIUploadAdapter:
    def test_save(self, tmp_path: Path) -> None:
        from app.infra.upload_adapter import FastAPIUploadAdapter

        content = b"hello world"
        file_obj = UploadFile(
            filename="test.txt",
            file=BytesIO(content),
            headers=Headers({"content-type": "text/plain"}),
        )
        adapter = FastAPIUploadAdapter(file_obj)
        assert adapter.filename == "test.txt"
        assert adapter.mimetype == "text/plain"

        dest = str(tmp_path / "output.txt")
        adapter.save(dest)
        with open(dest, "rb") as f:
            assert f.read() == content


class TestBase64UploadAdapter:
    def test_data_uri_png(self, tmp_path: Path) -> None:
        from app.infra.upload_adapter import Base64UploadAdapter

        raw = b"\x89PNG\r\n"
        b64 = base64.b64encode(raw).decode()
        data_uri = f"data:image/png;base64,{b64}"

        adapter = Base64UploadAdapter(data_uri)
        assert adapter.mimetype == "image/png"
        assert adapter.filename is not None and adapter.filename.endswith(".png")

        dest = str(tmp_path / "out.png")
        adapter.save(dest)
        with open(dest, "rb") as f:
            assert f.read() == raw

    def test_bare_base64(self, tmp_path: Path) -> None:
        from app.infra.upload_adapter import Base64UploadAdapter

        raw = b"binary data"
        b64 = base64.b64encode(raw).decode()

        adapter = Base64UploadAdapter(b64)
        assert adapter.mimetype == "application/octet-stream"

        dest = str(tmp_path / "out.bin")
        adapter.save(dest)
        with open(dest, "rb") as f:
            assert f.read() == raw

    def test_custom_filename(self) -> None:
        from app.infra.upload_adapter import Base64UploadAdapter

        b64 = base64.b64encode(b"data").decode()
        adapter = Base64UploadAdapter(b64, filename="custom.jpg")
        assert adapter.filename == "custom.jpg"

    def test_filename_without_extension(self) -> None:
        from app.infra.upload_adapter import Base64UploadAdapter

        b64 = base64.b64encode(b"data").decode()
        data_uri = f"data:image/jpeg;base64,{b64}"
        adapter = Base64UploadAdapter(data_uri, filename="photo")
        assert adapter.filename is not None and "." in adapter.filename  # 应自动补扩展名

    def test_content_type_property(self) -> None:
        from app.infra.upload_adapter import Base64UploadAdapter

        b64 = base64.b64encode(b"x").decode()
        adapter = Base64UploadAdapter(f"data:image/gif;base64,{b64}")
        assert adapter.content_type == "image/gif"


# 大语言模型配置


class TestLLMConfig:
    def test_is_model_config_sys_dict_key(self) -> None:
        from app.infra.llm.config import is_model_config_sys_dict_key

        assert is_model_config_sys_dict_key("chat_api_url") is True
        assert is_model_config_sys_dict_key("CHAT_API_KEY") is True
        assert is_model_config_sys_dict_key("emb_model_name") is True
        assert is_model_config_sys_dict_key("random_key") is False
        assert is_model_config_sys_dict_key(None) is False
        assert is_model_config_sys_dict_key("") is False

    def test_get_chat_model_config_defaults(self) -> None:
        from app.infra.llm.config import get_chat_model_config

        config = get_chat_model_config()
        assert "api" in config
        assert "key" in config
        assert "model" in config

    def test_get_embedding_model_config(self) -> None:
        from app.infra.llm.config import get_embedding_model_config

        config = get_embedding_model_config()
        assert "api" in config
        assert "model" in config

    def test_get_vl_model_config(self) -> None:
        from app.infra.llm.config import get_vl_model_config

        config = get_vl_model_config()
        assert "api" in config
        assert "model" in config

    def test_get_rerank_model_config(self) -> None:
        from app.infra.llm.config import get_rerank_model_config

        config = get_rerank_model_config()
        assert "api" in config
        assert "model" in config

    def test_get_rerank_top_k_default(self) -> None:
        from app.infra.llm.config import get_rerank_top_k

        top_k = get_rerank_top_k()
        assert isinstance(top_k, int)
        assert top_k > 0

    def test_read_env_with_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.infra.llm.config import get_chat_model_config

        monkeypatch.setenv("CHAT_API_URL", "http://custom:8080/v1")
        monkeypatch.setenv("CHAT_API_MODEL", "custom-model")
        config = get_chat_model_config()
        assert config["api"] == "http://custom:8080/v1"
        assert config["model"] == "custom-model"
