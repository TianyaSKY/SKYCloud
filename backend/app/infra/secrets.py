"""校验配置的密钥而不打印或记录其明文取值."""

import os
from urllib.parse import unquote, urlsplit

from sqlalchemy.engine import URL

DEFAULT_PASSWORDS = {
    "",
    "pwd",
    "guest",
    "skycloud",
    "minioadmin",
    "admin123",
    "changeme",
    "password",
    "none",
    "null",
}
MIN_PASSWORD_LENGTH = 12
MIN_SECRET_LENGTH = 32


def configured_password(name: str, value: str | None) -> str:
    """要求非默认的部署密码, 同时不泄露其明文值."""
    if (
        value is None
        or len(value) < MIN_PASSWORD_LENGTH
        or value.strip().lower() in DEFAULT_PASSWORDS
        or value.lower().startswith("replace-with")
    ):
        message = (
            f"{name} must be explicitly configured and cannot use a default password"
        )
        raise RuntimeError(message)
    return value


def check_deployment_secrets(database_url: URL) -> None:
    """校验独立的服务密钥与配置的基础设施密码."""
    keys = [
        required_secret(name)
        for name in (
            "SECRET_KEY",
            "OPENCODE_SERVER_SECRET",
            "RUNTIME_CONTROLLER_TOKEN",
        )
    ]
    if len(set(keys)) != len(keys):
        message = (
            "Signing, Runtime Controller and OpenCode secrets must be independent"
        )
        raise RuntimeError(message)
    configured_password("DATABASE_URL password", database_url.password)
    rabbit = os.getenv("RABBITMQ_URL")
    password = (
        unquote(urlsplit(rabbit).password or "")
        if rabbit
        else os.getenv("RABBITMQ_PASSWORD")
    )
    configured_password("RabbitMQ password", password)
    configured_password("MINIO_SECRET_KEY", os.getenv("MINIO_SECRET_KEY"))


def required_secret(name: str) -> str:
    """要求显式配置的随机密钥, 同时不泄露其明文值."""
    value = os.getenv(name, "").strip()
    if (
        len(value) < MIN_SECRET_LENGTH
        or value.lower().startswith("replace-with")
        or value == "sky_cloud_secret_key_dev_only"
    ):
        message = (
            f"{name} must be an explicitly configured random secret "
            "of at least 32 characters"
        )
        raise RuntimeError(message)
    return value
