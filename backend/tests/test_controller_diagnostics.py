"""Controller policy/error responses and library logs omit private exception data."""
import io
import logging
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from runtime_controller.app import create_app
from runtime_controller.diagnostics import PrivateDiagnosticsHandler
from runtime_controller.engine import PolicyViolation
from tests.test_runtime_controller import settings

SECRET = 'private-controller-daemon-sentinel'


@pytest.mark.parametrize('error,status,detail', [
    (RuntimeError(SECRET), 500, 'Runtime controller operation failed'),
    (ValueError(SECRET), 500, 'Runtime controller operation failed'),
    (PolicyViolation(SECRET), 403, 'Runtime operation forbidden'),
])
def test_controller_failure_response_and_log_are_private(settings, caplog, error, status, detail):
    engine = Mock()
    engine.ensure_image.side_effect = error
    with TestClient(create_app(settings, engine), raise_server_exceptions=False) as client:
        response = client.post('/image/ensure', headers={'X-Runtime-Controller-Token':settings.token})
    assert response.status_code == status and response.json() == {'detail':detail}
    assert SECRET not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    if status == 500:
        assert 'error_type=' + type(error).__name__ in caplog.text


def test_library_traceback_and_cached_exception_text_are_removed_without_mutating_record():
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
def test_access_log_omits_peer_path_and_untrusted_fields(fields, expected):
    output = io.StringIO()
    handler = PrivateDiagnosticsHandler(output)
    record = logging.LogRecord('uvicorn.access', logging.INFO, __file__, 1,
                               '%s %s %s %s %s', fields, None)
    record.stack_info = SECRET
    handler.handle(record)
    assert output.getvalue() == 'Controller HTTP request (' + expected + ')\n'
    assert SECRET not in output.getvalue()
    assert record.args == fields and record.stack_info == SECRET
