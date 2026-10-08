"""Bounded subprocess execution for private file converters."""
import os
import signal
import subprocess
from loguru import logger


def run_converter(command, *, timeout):
    """Discard converter output; terminate its POSIX group on interrupted waits."""
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, start_new_session=os.name == 'posix')
    try:
        returncode = process.wait(timeout=timeout)
    except BaseException:
        # A separate session keeps the worker and unrelated conversions out of
        # the group. Kill before reaping the leader so its ID remains owned.
        try:
            if os.name == 'posix':
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        except OSError as exc:
            logger.error('Converter termination failed (error_type={})', type(exc).__name__)
        try:
            process.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.error('Converter reap failed (error_type={})', type(exc).__name__)
        raise
    if returncode:
        raise subprocess.CalledProcessError(returncode, command)
    return subprocess.CompletedProcess(command, returncode)
