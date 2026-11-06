"""转换器超时后真实 POSIX 后代进程清理测试."""
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from app.infra.processes import run_converter
from tests.test_llm_safe_diagnostics import PRIVATE, logs

_ = logs  # Pytest fixture import

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='POSIX process group ownership')


def test_timeout_stops_grandchild_without_harming_independent_conversion(tmp_path: Path) -> None:
    marker = tmp_path / 'surviving-child.txt'
    ready = tmp_path / 'child-ready.txt'
    # 若后代进程在进程组终止后幸存, 则会在超时截止时间后写入.
    child = (
        f"from pathlib import Path; import time; Path({str(ready)!r}).write_text('ready'); "
        f"time.sleep(1); Path({str(marker)!r}).write_text('survived'); time.sleep(30)"
    )
    parent = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child!r}]); time.sleep(30)"
    unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            run_converter([sys.executable, '-c', parent], timeout=0.5)
        assert ready.exists(), 'descendant was not started before timeout'
        assert unrelated.poll() is None
        # 跨越子进程写入边界; 这是一种可观测效应,
        # 而不是将平台特有的僵尸 PID 存在判定为存活.
        time.sleep(1.1)
        assert not marker.exists()
    finally:
        unrelated.kill()
        unrelated.wait(timeout=5)


def test_nonzero_exit_preserves_status_without_capturing_private_stderr(logs: list[tuple[str, Any]]) -> None:
    with pytest.raises(subprocess.CalledProcessError) as error:
        run_converter([sys.executable, '-c', f'import sys; sys.stderr.write({PRIVATE!r}); sys.exit(7)'], timeout=2)
    assert error.value.returncode == 7 and error.value.stderr is None and error.value.stdout is None


def test_successful_process_returns_completion() -> None:
    result = run_converter([sys.executable, '-c', 'pass'], timeout=2)
    assert result.returncode == 0 and result.stdout is None and result.stderr is None
