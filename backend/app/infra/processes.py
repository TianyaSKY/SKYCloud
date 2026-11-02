"""私有文件转换器的有界子进程执行工具."""

import os
import signal
import subprocess
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from collections.abc import Sequence


def run_converter(
    command: "Sequence[str]",
    *,
    timeout: float | None,
) -> subprocess.CompletedProcess[None]:
    """丢弃转换器输出; 在等待中断时终止其 POSIX 进程组."""
    process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=os.name == "posix",
    )
    try:
        returncode = process.wait(timeout=timeout)
    except BaseException:
        # 独立的 session 使 worker 和不相关的转换操作脱离该进程组。
        # 在回收 leader 前杀死进程, 保证其 ID 仍被持有。
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        except OSError as exc:
            logger.error(
                "Converter termination failed (error_type={})",
                type(exc).__name__,
            )
        try:
            process.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.error("Converter reap failed (error_type={})", type(exc).__name__)
        raise
    if returncode:
        raise subprocess.CalledProcessError(returncode, command)
    result: subprocess.CompletedProcess[None] = subprocess.CompletedProcess(
        command,
        returncode,
    )
    return result
