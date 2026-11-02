"""集成测试套件的诊断与 fixture 文件路径解析."""
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
