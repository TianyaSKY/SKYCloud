"""Validate configured secrets without logging their values."""

import os

DEFAULT_PASSWORDS = {"", "pwd", "guest", "skycloud", "minioadmin", "admin123", "changeme", "password", "none", "null"}


def configured_password(name: str, value: str | None) -> str:
    if value is None or len(value) < 12 or value.strip().lower() in DEFAULT_PASSWORDS or value.lower().startswith("replace-with"):
        raise RuntimeError(f"{name} must be explicitly configured and cannot use a default password")
    return value


def check_deployment_secrets(database_url):
    from urllib.parse import urlsplit, unquote
    keys = [required_secret(name) for name in ("SECRET_KEY", "OPENCODE_SERVER_SECRET", "RUNTIME_CONTROLLER_TOKEN")]
    if len(set(keys)) != len(keys):
        raise RuntimeError("Signing, Runtime Controller and OpenCode secrets must be independent")
    configured_password("DATABASE_URL password", database_url.password)
    rabbit = os.getenv("RABBITMQ_URL")
    password = unquote(urlsplit(rabbit).password or "") if rabbit else os.getenv("RABBITMQ_PASSWORD")
    configured_password("RabbitMQ password", password)
    configured_password("MINIO_SECRET_KEY", os.getenv("MINIO_SECRET_KEY"))


def required_secret(name: str) -> str:
    value = os.getenv(name, "").strip()
    if (
        len(value) < 32
        or value.lower().startswith("replace-with")
        or value == "sky_cloud_secret_key_dev_only"
    ):
        raise RuntimeError(f"{name} must be an explicitly configured random secret of at least 32 characters")
    return value
