"""Diagnostic and fixture files for the opt-in integration harnesses.

Docker Desktop hosts provide /private/tmp; Linux runners use the system temp
directory. SKYCLOUD_TEST_FIXTURE_DIR overrides both, and the directory is created
on demand so failure-path diagnostics never mask the real assertion.
"""
import os
import tempfile
from pathlib import Path


def fixture_dir() -> Path:
    configured = os.getenv('SKYCLOUD_TEST_FIXTURE_DIR')
    if configured:
        path = Path(configured)
    else:
        legacy = Path('/private/tmp')
        path = legacy if legacy.is_dir() else Path(tempfile.gettempdir())
    path.mkdir(parents=True, exist_ok=True)
    return path


FIXTURE_DIR = fixture_dir()
