"""Unit tests for scripts/migrate-env.py and scripts/start.py."""

import importlib.util
from pathlib import Path
import stat
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def _load_script_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        msg = f"Cannot load {path}"
        raise RuntimeError(msg)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestMigrateEnvScript:
    """Tests for legacy environment migration script."""

    def test_migrate_missing_file_fails(self, tmp_path: Path) -> None:
        mig_mod = _load_script_module("migrate_env", ROOT / "scripts/migrate-env.py")
        missing_env = tmp_path / "nonexistent.env"
        ret = mig_mod.migrate(env_file=missing_env)
        assert ret == 1

    def test_migrate_already_streamlined_is_noop(self, tmp_path: Path) -> None:
        mig_mod = _load_script_module("migrate_env", ROOT / "scripts/migrate-env.py")
        env_file = tmp_path / ".env"
        env_file.write_text(
            "CHAT_API_URL=https://chat.example.com\n"
            "CHAT_API_KEY=sk-chat\n"
            "CHAT_API_MODEL=gpt-4o\n"
            "EMB_API_URL=https://emb.example.com\n"
            "EMB_API_KEY=sk-emb\n"
            "EMB_MODEL_NAME=text-embedding-3-small\n",
            encoding="utf-8",
        )
        backup_file = tmp_path / ".env.bak"
        ret = mig_mod.migrate(
            env_file=env_file,
            backup_file=backup_file,
        )
        assert ret == 0
        assert not backup_file.exists()

    def test_migrate_full_legacy_env(self, tmp_path: Path) -> None:
        mig_mod = _load_script_module("migrate_env", ROOT / "scripts/migrate-env.py")
        legacy_content = (
            "# Legacy .env with 86 items\n"
            "POSTGRES_PASSWORD=my_existing_postgres_db_pw_123456\n"
            "RABBITMQ_PASSWORD=my_existing_rabbit_pw_123456\n"
            "MINIO_SECRET_KEY=my_existing_minio_pw_123456\n"
            "SECRET_KEY=jwt_secret_token_at_least_32_characters_long_1\n"
            "RUNTIME_CONTROLLER_TOKEN=ctrl_secret_token_at_least_32_chars_2\n"
            "OPENCODE_SERVER_SECRET=open_secret_token_at_least_32_chars_3\n"
            "MINIO_IMAGE=sha256:customminioimage\n"
            "OPENCODE_IMAGE=sha256:customruntimeimage\n"
            "CHAT_API_URL=https://chat.legacy.com\n"
            "CHAT_API_KEY=chat-secret-key\n"
            "CHAT_API_MODEL=gpt-4o\n"
            "EMB_API_URL=https://emb.legacy.com\n"
            "EMB_API_KEY=emb-secret-key\n"
            "EMB_MODEL_NAME=text-embedding-3\n"
            "FRONTEND_PORT=8080\n"
            "UPLOAD_MAX_FILE_BYTES=52428800\n"
            "DEFAULT_MODEL_PWD=legacy_pwd_to_remove\n"
        )
        env_file = tmp_path / ".env"
        env_file.write_text(legacy_content, encoding="utf-8")
        gen_file = tmp_path / ".skycloud/generated.env"
        adv_file = tmp_path / ".env.advanced"
        bak_file = tmp_path / ".env.bak"

        ret = mig_mod.migrate(
            env_file=env_file,
            generated_env_file=gen_file,
            advanced_env_file=adv_file,
            backup_file=bak_file,
        )
        assert ret == 0

        # 1. Backup matches legacy content
        assert bak_file.read_text(encoding="utf-8") == legacy_content

        # 2. Generated env has system secrets and image IDs, with mode 0600
        assert gen_file.is_file()
        assert stat.S_IMODE(gen_file.stat().st_mode) == 0o600
        gen_lines = dict(
            line.split("=", 1)
            for line in gen_file.read_text(encoding="utf-8").splitlines()
            if "=" in line and not line.startswith("#")
        )
        assert gen_lines["POSTGRES_PASSWORD"] == "my_existing_postgres_db_pw_123456"
        assert gen_lines["RABBITMQ_PASSWORD"] == "my_existing_rabbit_pw_123456"
        assert gen_lines["MINIO_SECRET_KEY"] == "my_existing_minio_pw_123456"
        assert gen_lines["SECRET_KEY"] == "jwt_secret_token_at_least_32_characters_long_1"
        assert gen_lines["MINIO_IMAGE"] == "sha256:customminioimage"
        assert gen_lines["OPENCODE_IMAGE"] == "sha256:customruntimeimage"

        # 3. New .env only contains the 6 model keys and does NOT contain secrets or DEFAULT_MODEL_PWD
        new_env_lines = dict(
            line.split("=", 1)
            for line in env_file.read_text(encoding="utf-8").splitlines()
            if "=" in line and not line.startswith("#")
        )
        assert set(new_env_lines.keys()) == {
            "CHAT_API_URL",
            "CHAT_API_KEY",
            "CHAT_API_MODEL",
            "EMB_API_URL",
            "EMB_API_KEY",
            "EMB_MODEL_NAME",
        }
        assert new_env_lines["CHAT_API_KEY"] == "chat-secret-key"
        assert "POSTGRES_PASSWORD" not in new_env_lines
        assert "DEFAULT_MODEL_PWD" not in new_env_lines

        # 4. Advanced env holds non-default overrides, and DEFAULT_MODEL_PWD is dropped
        assert adv_file.is_file()
        adv_lines = dict(
            line.split("=", 1)
            for line in adv_file.read_text(encoding="utf-8").splitlines()
            if "=" in line and not line.startswith("#")
        )
        assert adv_lines["FRONTEND_PORT"] == "8080"
        assert adv_lines["UPLOAD_MAX_FILE_BYTES"] == "52428800"
        assert "DEFAULT_MODEL_PWD" not in adv_lines


class TestStartScript:
    """Tests for unified startup entrypoint script."""

    def test_ensure_generated_env_creation(self, tmp_path: Path) -> None:
        gen_mod = _load_script_module("generate_env", ROOT / "scripts/generate-env.py")
        gen_file = tmp_path / "generated.env"
        ret_path = gen_mod.ensure_generated_env(gen_file)
        assert ret_path == gen_file
        assert gen_file.is_file()
        assert stat.S_IMODE(gen_file.stat().st_mode) == 0o600

        # Repeated run doesn't overwrite
        mtime_before = gen_file.stat().st_mtime_ns
        ret_path2 = gen_mod.ensure_generated_env(gen_file)
        assert ret_path2 == gen_file
        assert gen_file.stat().st_mtime_ns == mtime_before

    def test_start_script_check_only_passes_with_valid_configs(
        self, tmp_path: Path,
    ) -> None:
        start_mod = _load_script_module("start_script", ROOT / "scripts/start.py")

        user_env = tmp_path / ".env"
        user_env.write_text(
            "CHAT_API_URL=https://chat.example.com\n"
            "CHAT_API_KEY=chat-secret\n"
            "CHAT_API_MODEL=gpt-4o\n"
            "EMB_API_URL=https://emb.example.com\n"
            "EMB_API_KEY=emb-secret\n"
            "EMB_MODEL_NAME=text-embedding-3-small\n",
            encoding="utf-8",
        )
        gen_env = tmp_path / ".skycloud/generated.env"
        adv_env = tmp_path / ".env.advanced"

        # Mock is_image_present to avoid docker engine calls during test
        with patch.object(start_mod, "is_image_present", return_value=True):
            exit_code = start_mod.main([
                "--check-only",
                "--env-file",
                str(user_env),
                "--generated-env-file",
                str(gen_env),
                "--advanced-env-file",
                str(adv_env),
            ])
            assert exit_code == 0
            assert gen_env.is_file()
            assert stat.S_IMODE(gen_env.stat().st_mode) == 0o600

    def test_start_script_fails_if_model_key_missing(self, tmp_path: Path) -> None:
        start_mod = _load_script_module("start_script", ROOT / "scripts/start.py")

        user_env = tmp_path / ".env"
        # Missing EMB_API_KEY
        user_env.write_text(
            "CHAT_API_URL=https://chat.example.com\n"
            "CHAT_API_KEY=chat-secret\n"
            "CHAT_API_MODEL=gpt-4o\n"
            "EMB_API_URL=https://emb.example.com\n"
            "EMB_API_KEY=\n"
            "EMB_MODEL_NAME=text-embedding-3-small\n",
            encoding="utf-8",
        )
        gen_env = tmp_path / ".skycloud/generated.env"
        adv_env = tmp_path / ".env.advanced"

        exit_code = start_mod.main([
            "--check-only",
            "--env-file",
            str(user_env),
            "--generated-env-file",
            str(gen_env),
            "--advanced-env-file",
            str(adv_env),
        ])
        assert exit_code == 1
