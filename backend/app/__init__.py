"""应用启动时校验架构, 数据库初始化在独立进程中运行."""

from pathlib import Path

from app.features.auth.legacy_admin import check_legacy_admin
from app.infra.extensions import UPLOAD_FOLDER, engine
from app.infra.logging_setup import setup_logging
from app.infra.schema import check_schema
from app.infra.secrets import check_deployment_secrets

setup_logging()


def initialize_application() -> None:
    """校验部署配置并准备上传目录."""
    check_deployment_secrets(engine.url)
    check_schema(engine)
    check_legacy_admin(engine)
    Path(UPLOAD_FOLDER).mkdir(parents=True, exist_ok=True)

