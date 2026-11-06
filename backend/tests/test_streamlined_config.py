"""Tests for streamlined typed configurations and LLM degradation behavior."""

from unittest.mock import MagicMock, patch
import pytest

from app.config.assistant import AssistantConfig, get_assistant_config
from app.config.database import (
    DatabasePoolConfig,
    RedisPoolConfig,
    get_database_pool_config,
    get_redis_pool_config,
)
from app.config.mcp import McpConfig, get_mcp_config
from app.config.rag import RagConfig, get_rag_config
from app.config.runtime import (
    RuntimeLlmConfig,
    WorkspaceResourceConfig,
    get_runtime_llm_config,
    get_workspace_resource_config,
)
from app.config.storage import MinioConfig, get_minio_config
from app.config.upload import (
    DEFAULT_FILE_LIMIT,
    DEFAULT_USER_CAPACITY,
    UploadLimitsConfig,
    UploadQuotaConfig,
    get_upload_limits_config,
    get_upload_quota_config,
    positive_limit,
)
from app.config.worker import WorkerConfig, get_worker_config
from app.infra.llm.config import (
    chat_supports_vision,
    get_rerank_model_config,
    get_vl_model_config,
)


class TestDatabaseConfig:
    """Tests for database and redis pool configurations."""

    def test_database_pool_defaults(self) -> None:
        cfg = DatabasePoolConfig()
        assert cfg.pool_size == 10
        assert cfg.max_overflow == 20
        assert cfg.pool_timeout == 30
        assert cfg.pool_recycle == 1800
        assert cfg.connect_timeout == 5

    @pytest.mark.parametrize(
        ("size", "overflow", "timeout", "recycle"),
        [
            (0, 10, 30, 1800),
            (10, -1, 30, 1800),
            (10, 10, 0, 1800),
            (10, 10, 30, 0),
        ],
    )
    def test_database_pool_invalid_bounds(
        self, size: int, overflow: int, timeout: int, recycle: int,
    ) -> None:
        with pytest.raises(ValueError):
            DatabasePoolConfig(
                pool_size=size,
                max_overflow=overflow,
                pool_timeout=timeout,
                pool_recycle=recycle,
            )

    def test_database_pool_env_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DB_POOL_SIZE", "15")
        monkeypatch.setenv("DB_MAX_OVERFLOW", "25")
        monkeypatch.setenv("DB_POOL_TIMEOUT", "45")
        monkeypatch.setenv("DB_POOL_RECYCLE", "3600")
        cfg = get_database_pool_config()
        assert cfg.pool_size == 15
        assert cfg.max_overflow == 25
        assert cfg.pool_timeout == 45
        assert cfg.pool_recycle == 3600

    def test_redis_pool_defaults_and_validation(self) -> None:
        cfg = RedisPoolConfig()
        assert cfg.connect_timeout == 3.0
        assert cfg.socket_timeout == 5.0

        with pytest.raises(ValueError):
            RedisPoolConfig(connect_timeout=0.0)
        with pytest.raises(ValueError):
            RedisPoolConfig(socket_timeout=0.0)

    def test_redis_pool_env_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REDIS_CONNECT_TIMEOUT", "6.0")
        monkeypatch.setenv("REDIS_SOCKET_TIMEOUT", "10.0")
        cfg = get_redis_pool_config()
        assert cfg.connect_timeout == 6.0
        assert cfg.socket_timeout == 10.0


class TestUploadConfig:
    """Tests for upload limit and quota configurations."""

    def test_upload_limits_defaults_and_validation(self) -> None:
        cfg = UploadLimitsConfig()
        assert cfg.max_file_size == DEFAULT_FILE_LIMIT
        assert cfg.chunk_size == 2 * 1024 * 1024

        with pytest.raises(ValueError):
            UploadLimitsConfig(max_file_size=0)
        with pytest.raises(ValueError):
            UploadLimitsConfig(chunk_size=0)

    def test_upload_quota_defaults_and_validation(self) -> None:
        cfg = UploadQuotaConfig()
        assert cfg.user_capacity == DEFAULT_USER_CAPACITY
        assert cfg.daily_attempts == 200

        with pytest.raises(ValueError):
            UploadQuotaConfig(user_capacity=0)
        with pytest.raises(ValueError):
            UploadQuotaConfig(daily_attempts=0)

    def test_positive_limit_helper(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_UPLOAD_VAR", "100")
        assert positive_limit("TEST_UPLOAD_VAR", 50) == 100

        monkeypatch.delenv("TEST_UPLOAD_VAR", raising=False)
        assert positive_limit("TEST_UPLOAD_VAR", 50) == 50

        monkeypatch.setenv("TEST_UPLOAD_VAR", "invalid")
        with pytest.raises(RuntimeError, match="positive integer"):
            positive_limit("TEST_UPLOAD_VAR", 50)

        monkeypatch.setenv("TEST_UPLOAD_VAR", "-5")
        with pytest.raises(RuntimeError, match="positive integer"):
            positive_limit("TEST_UPLOAD_VAR", 50)

        monkeypatch.setenv("TEST_UPLOAD_VAR", "0")
        with pytest.raises(RuntimeError, match="positive integer"):
            positive_limit("TEST_UPLOAD_VAR", 50)

    def test_upload_env_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("UPLOAD_MAX_FILE_SIZE", "52428800")
        monkeypatch.setenv("UPLOAD_USER_CAPACITY", "2147483648")
        limits = get_upload_limits_config()
        quota = get_upload_quota_config()
        assert limits.max_file_size == 52428800
        assert quota.user_capacity == 2147483648


class TestWorkerConfig:
    """Tests for worker concurrency configurations."""

    def test_worker_defaults_preserves_old_env_example(self) -> None:
        cfg = WorkerConfig()
        # Old .env.example had 20 threads; ensure our default matches
        assert cfg.max_threads == 20
        assert cfg.organize_threads == 1
        assert cfg.document_conversion_workers == 2
        assert cfg.video_processing_workers == 2

    def test_worker_validation(self) -> None:
        with pytest.raises(ValueError):
            WorkerConfig(max_threads=0)
        with pytest.raises(ValueError):
            WorkerConfig(organize_threads=0)

    def test_worker_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WORKER_MAX_THREADS", "8")
        monkeypatch.setenv("WORKER_ORGANIZE_THREADS", "4")
        cfg = get_worker_config()
        assert cfg.max_threads == 8
        assert cfg.organize_threads == 4


class TestRagConfig:
    """Tests for RAG pipeline parameters."""

    def test_rag_defaults_and_validation(self) -> None:
        cfg = RagConfig()
        assert cfg.vector_fetch_k == 20
        assert cfg.rerank_top_k == 8
        assert cfg.rrf_k == 60
        assert cfg.multi_query_max_queries == 3
        assert cfg.fusion_top_k == 30

        with pytest.raises(ValueError):
            RagConfig(vector_fetch_k=0)
        with pytest.raises(ValueError):
            RagConfig(rerank_top_k=0)
        with pytest.raises(ValueError):
            RagConfig(rrf_k=0)

    def test_rag_env_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RAG_VECTOR_FETCH_K", "30")
        monkeypatch.setenv("RERANK_TOP_K", "12")
        cfg = get_rag_config()
        assert cfg.vector_fetch_k == 30
        assert cfg.rerank_top_k == 12


class TestRuntimeAndAssistantConfigs:
    """Tests for runtime controller, workspace, storage, assistant, and MCP configs."""

    def test_runtime_llm_and_workspace_defaults(self) -> None:
        r_cfg = get_runtime_llm_config()
        assert r_cfg.token_ttl_seconds == 3600
        assert r_cfg.hourly_token_budget == 100000
        assert r_cfg.hourly_request_limit == 60
        assert r_cfg.max_output_tokens == 4096
        assert r_cfg.context_tokens == 65536

        w_cfg = get_workspace_resource_config()
        assert w_cfg.mem_limit == "1g"
        assert w_cfg.cpu_quota == 100000
        assert w_cfg.max_workspaces_per_user == 3

        with pytest.raises(ValueError):
            RuntimeLlmConfig(token_ttl_seconds=0)
        with pytest.raises(ValueError):
            WorkspaceResourceConfig(mem_limit="")

    def test_assistant_and_mcp_defaults(self) -> None:
        a_cfg = get_assistant_config()
        assert a_cfg.v2_enabled is True
        assert a_cfg.expert_enabled is True
        assert a_cfg.permission_ui_enabled is True
        assert a_cfg.runtime_lock_ttl == 3600
        assert a_cfg.abandoned_run_grace_seconds == 45

        m_cfg = get_mcp_config()
        assert m_cfg.port == 5001
        assert m_cfg.request_concurrency == 4

        with pytest.raises(ValueError):
            AssistantConfig(runtime_lock_ttl=0)
        with pytest.raises(ValueError):
            McpConfig(port=0)

    def test_minio_defaults(self) -> None:
        m_cfg = get_minio_config()
        assert m_cfg.bucket == "skycloud"
        assert m_cfg.connect_timeout == 5.0
        assert m_cfg.retries == 2

        with pytest.raises(ValueError):
            MinioConfig(bucket="")
        with pytest.raises(ValueError):
            MinioConfig(connect_timeout=0.0)


class TestLlmDegradationAndVisionSupport:
    """Tests for vision support detection and graceful VL/Rerank fallback."""

    @pytest.mark.parametrize(
        ("model_name", "expected"),
        [
            ("gpt-4o", True),
            ("gpt-4o-mini", True),
            ("gpt-4-turbo", True),
            ("claude-3-5-sonnet-20241022", True),
            ("claude-3-opus", True),
            ("gemini-1.5-pro", True),
            ("gemini-2.0-flash", True),
            ("qwen-vl-max", True),
            ("qwen2.5-vl-72b", True),
            ("internvl2-8b", True),
            ("glm-4v-plus", True),
            ("deepseek-vl-7b", True),
            ("gpt-3.5-turbo", False),
            ("gpt-4", False),
            ("qwen-plus", False),
            ("deepseek-chat", False),
            ("", False),
        ],
    )
    def test_chat_supports_vision(self, model_name: str, expected: bool) -> None:
        assert chat_supports_vision(model_name) is expected

    def test_vl_config_explicit_setting_takes_precedence(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CHAT_API_URL", "https://chat.api.com")
        monkeypatch.setenv("CHAT_API_KEY", "chat-key")
        monkeypatch.setenv("CHAT_API_MODEL", "gpt-4o")

        monkeypatch.setenv("VL_API_URL", "https://vl.api.com")
        monkeypatch.setenv("VL_API_KEY", "vl-key")
        monkeypatch.setenv("VL_API_MODEL", "custom-vl-model")

        vl_cfg = get_vl_model_config()
        assert vl_cfg["api"] == "https://vl.api.com"
        assert vl_cfg["key"] == "vl-key"
        assert vl_cfg["model"] == "custom-vl-model"

    def test_vl_config_reuses_chat_model_if_vision_supported(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CHAT_API_URL", "https://chat.api.com")
        monkeypatch.setenv("CHAT_API_KEY", "chat-key")
        monkeypatch.setenv("CHAT_API_MODEL", "gpt-4o")

        monkeypatch.delenv("VL_API_KEY", raising=False)
        monkeypatch.delenv("VL_API_MODEL", raising=False)
        monkeypatch.delenv("VL_API_URL", raising=False)

        vl_cfg = get_vl_model_config()
        assert vl_cfg["api"] == "https://chat.api.com"
        assert vl_cfg["key"] == "chat-key"
        assert vl_cfg["model"] == "gpt-4o"

    def test_vl_config_returns_empty_key_if_chat_model_cannot_see(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CHAT_API_URL", "https://chat.api.com")
        monkeypatch.setenv("CHAT_API_KEY", "chat-key")
        monkeypatch.setenv("CHAT_API_MODEL", "gpt-3.5-turbo")

        monkeypatch.delenv("VL_API_KEY", raising=False)
        monkeypatch.delenv("VL_API_MODEL", raising=False)
        monkeypatch.delenv("VL_API_URL", raising=False)

        vl_cfg = get_vl_model_config()
        assert vl_cfg["key"] == ""

    def test_rerank_config_absent_skips_cleanly(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("RERANK_API_KEY", raising=False)
        cfg = get_rerank_model_config()
        assert cfg["key"] == ""

        monkeypatch.setenv("RERANK_API_KEY", "")
        cfg = get_rerank_model_config()
        assert cfg["key"] == ""

    def test_rerank_config_present_returns_config(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("RERANK_API_KEY", "secret-rerank-key")
        monkeypatch.setenv("RERANK_API_URL", "https://rerank.api.com")
        monkeypatch.setenv("RERANK_MODEL", "bge-reranker-v2")
        cfg = get_rerank_model_config()
        assert cfg["key"] == "secret-rerank-key"
        assert cfg["api"] == "https://rerank.api.com"
        assert cfg["model"] == "bge-reranker-v2"


class TestFolderDescriptionDegradation:
    """Tests for generate_file_description behavior when VL model is absent."""

    def test_degrades_to_chat_for_text_extractable_file_without_vl(self) -> None:
        from app.features.folder.organize.description import generate_file_description

        mock_choice = MagicMock()
        mock_choice.message.content = "Extracted summary of quarterly earnings."
        mock_resp = MagicMock(choices=[mock_choice])

        vl_cfg = {"api": "https://api.com", "key": "", "model": "none"}
        chat_cfg = {"api": "https://api.com", "key": "chat-key", "model": "gpt-4o"}

        with (
            patch(
                "app.features.folder.organize.description._extract_text_content",
                return_value="Quarterly earnings revenue was up 10 percent.",
            ),
            patch(
                "app.features.folder.organize.description.sync_client.chat_completion",
                return_value=mock_resp,
            ),
        ):
            desc = generate_file_description(
                local_path="/storage/report.pdf",
                config=vl_cfg,
                chat_config=chat_cfg,
                user_id=1,
            )
            assert "Extracted summary" in desc

    def test_pure_visual_file_without_vl_does_not_fake_analysis(self) -> None:
        from app.features.folder.organize.description import generate_file_description

        vl_cfg = {"api": "https://api.com", "key": "", "model": "none"}
        chat_cfg = {"api": "https://api.com", "key": "chat-key", "model": "gpt-3.5"}

        with patch(
            "app.features.folder.organize.description._extract_text_content",
            return_value="",
        ):
            desc = generate_file_description(
                local_path="/storage/architecture.png",
                config=vl_cfg,
                chat_config=chat_cfg,
                user_id=1,
            )
            assert "architecture.png" in desc
            assert "未配置多模态视觉模型" in desc


class TestOriginPermissiveDefaults:
    """Tests that APP_ORIGIN defaults to permitting all origins without configuration."""

    def test_origin_defaults_to_wildcard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.features.auth import refresh_sessions

        monkeypatch.delenv("APP_ORIGIN", raising=False)
        assert refresh_sessions.origin() == "*"

        monkeypatch.setenv("APP_ORIGIN", "")
        assert refresh_sessions.origin() == "*"

        monkeypatch.setenv("APP_ORIGIN", "*")
        assert refresh_sessions.origin() == "*"

    def test_check_origin_permits_any_origin_when_unset(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from fastapi import Request
        from app.features.auth import refresh_sessions

        monkeypatch.delenv("APP_ORIGIN", raising=False)
        scope = {
            "type": "http",
            "headers": [
                (b"origin", b"http://localhost:5173"),
                (b"x-skycloud-session", b"1"),
            ],
        }
        req = Request(scope)  # pyright: ignore[reportArgumentType]
        # Should not raise HTTPException 403
        refresh_sessions.check_origin(req, required=True)

        scope2 = {
            "type": "http",
            "headers": [(b"origin", b"https://arbitrary.domain.com:8443")],
        }
        req2 = Request(scope2)  # pyright: ignore[reportArgumentType]
        refresh_sessions.check_origin(req2, required=False)

    def test_check_origin_enforces_when_explicitly_configured(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from fastapi import HTTPException, Request
        from app.features.auth import refresh_sessions

        monkeypatch.setenv("APP_ORIGIN", "http://app.skycloud.localhost")
        assert refresh_sessions.origin() == "http://app.skycloud.localhost"

        # Matching origin succeeds
        scope_ok = {
            "type": "http",
            "headers": [
                (b"origin", b"http://app.skycloud.localhost"),
                (b"x-skycloud-session", b"1"),
            ],
        }
        refresh_sessions.check_origin(Request(scope_ok), required=True)  # pyright: ignore[reportArgumentType]

        # Mismatched origin fails with 403
        scope_bad = {
            "type": "http",
            "headers": [
                (b"origin", b"http://evil.com"),
                (b"x-skycloud-session", b"1"),
            ],
        }
        with pytest.raises(HTTPException) as exc_info:
            refresh_sessions.check_origin(Request(scope_bad), required=True)  # pyright: ignore[reportArgumentType]
        assert exc_info.value.status_code == 403
