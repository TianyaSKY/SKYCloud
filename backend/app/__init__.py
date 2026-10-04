"""Application startup validates schema; migrations run in a separate process."""

import os

from app.infra.logging_setup import setup_logging

setup_logging()

from app.infra.extensions import engine, UPLOAD_FOLDER
from app.infra.schema import check_schema
from app.infra.secrets import check_deployment_secrets
from app.features.auth.legacy_admin import check_legacy_admin


def initialize_application():
    check_deployment_secrets(engine.url)
    check_schema(engine)
    check_legacy_admin(engine)
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
