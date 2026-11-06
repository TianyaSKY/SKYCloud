"""控制器策略/错误响应与库日志省略私有异常数据测试."""
import io
import logging
from typing import Any
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from runtime_controller.app import create_app
from runtime_controller.diagnostics import PrivateDiagnosticsHandler
from runtime_controller.engine import PolicyViolation
from tests.test_runtime_controller import settings

_ = settings  # Pytest fixture import

SECRET = 'private-controller-daemon-sentinel'


@pytest.mark.parametrize('error,status,detail', [
    (RuntimeError(SECRET), 500, 'Runtime controller operation failed'),
    (ValueError(SECRET), 500, 'Runtime controller operation failed'),
    (PolicyViolation(SECRET), 403, 'Runtime operation forbidden'),
])
def test_controller_failure_response_and_log_are_private(
    settings: Any, caplog: pytest.LogCaptureFixture, error: Exception, status: int, detail: str
) -> None:
    engine = Mock()
    engine.prepare.side_effect = error
    with TestClient(create_app(settings, engine), raise_server_exceptions=False) as client:
        response = client.post(
            '/containers/prepare',
            headers={'X-Runtime-Controller-Token': settings.token},
            json={'workspace_id': 1, 'user_id': 1, 'runtime_id': 1, 'generation': 'a' * 32},
        )
    assert response.status_code == status and response.json() == {'detail': detail}
    assert SECRET not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    if status == 500:
        assert 'error_type=' + type(error).__name__ in caplog.text


def test_library_traceback_and_cached_exception_text_are_removed_without_mutating_record() -> None:
    output = io.StringIO()
    handler = PrivateDiagnosticsHandler(output)
    try:
        raise RuntimeError(SECRET)
    except RuntimeError:
        import sys
        record = logging.LogRecord('uvicorn.error', logging.ERROR, __file__, 1,
                                   SECRET, (), sys.exc_info())
    record.exc_text = SECRET
    record.stack_info = SECRET
    handler.handle(record)
    assert output.getvalue() == 'Library failure in uvicorn.error (error_type=RuntimeError)\n'
    assert record.msg == SECRET and record.exc_info is not None


@pytest.mark.parametrize('fields,expected', [
    ((SECRET, 'GET', '/' + SECRET, '1.1', 403), 'method=GET status=403'),
    ((SECRET, SECRET, SECRET, SECRET, SECRET), 'method=unknown status=unknown'),
    ((SECRET,), 'method=unknown status=unknown'),
])
def test_access_log_omits_peer_path_and_untrusted_fields(fields: tuple[object, ...], expected: str) -> None:
    output = io.StringIO()
    handler = PrivateDiagnosticsHandler(output)
    record = logging.LogRecord('uvicorn.access', logging.INFO, __file__, 1,
                               '%s %s %s %s %s', fields, None)
    record.stack_info = SECRET
    handler.handle(record)
    assert output.getvalue() == 'Controller HTTP request (' + expected + ')\n'
    assert SECRET not in output.getvalue()
    assert record.args == fields and record.stack_info == SECRET
