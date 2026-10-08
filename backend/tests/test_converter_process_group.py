"""Real POSIX descendant cleanup after converter timeout."""
import os
import subprocess
import sys
import time
import pytest
from app.infra.processes import run_converter
from tests.test_llm_safe_diagnostics import logs,assert_safe,PRIVATE

pytestmark=pytest.mark.skipif(os.name!='posix',reason='POSIX process group ownership')


def test_timeout_stops_grandchild_without_harming_independent_conversion(tmp_path):
    marker=tmp_path/'surviving-child.txt'
    ready=tmp_path/'child-ready.txt'
    # The descendant writes after the deadline if it survives the group kill.
    child=f"from pathlib import Path; import time; Path({str(ready)!r}).write_text('ready'); time.sleep(1); Path({str(marker)!r}).write_text('survived'); time.sleep(30)"
    parent=f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child!r}]); time.sleep(30)"
    unrelated=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],start_new_session=True)
    try:
        with pytest.raises(subprocess.TimeoutExpired):run_converter([sys.executable,'-c',parent],timeout=.5)
        assert ready.exists(),'descendant was not started before timeout'
        assert unrelated.poll() is None
        # Cross the child write boundary; this is an observable effect rather
        # than interpreting platform-specific zombie PID presence as survival.
        time.sleep(1.1)
        assert not marker.exists()
    finally:
        unrelated.kill();unrelated.wait(timeout=5)


def test_nonzero_exit_preserves_status_without_capturing_private_stderr(logs):
    with pytest.raises(subprocess.CalledProcessError) as error:
        run_converter([sys.executable,'-c',f'import sys; sys.stderr.write({PRIVATE!r}); sys.exit(7)'],timeout=2)
    assert error.value.returncode==7 and error.value.stderr is None and error.value.stdout is None


def test_successful_process_returns_completion():
    result=run_converter([sys.executable,'-c','pass'],timeout=2)
    assert result.returncode==0 and result.stdout is None and result.stderr is None
