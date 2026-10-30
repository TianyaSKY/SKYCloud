"""Application startup validates schema; migrations run in a separate process."""

from pathlib import Path

from app.features.auth.legacy_admin import check_legacy_admin
from app.infra.extensions import UPLOAD_FOLDER, engine
from app.infra.logging_setup import setup_logging
from app.infra.schema import check_schema
from app.infra.secrets import check_deployment_secrets

setup_logging()


def initialize_application() -> None:
    """Validate deployment and prepare the configured upload directory."""
    check_deployment_secrets(engine.url)
    check_schema(engine)
    check_legacy_admin(engine)
    Path(UPLOAD_FOLDER).mkdir(parents=True, exist_ok=True)
