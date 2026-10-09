"""Accounting failures must not alter the caller's business result or leak data."""
from unittest.mock import Mock
import pytest
from app.features.token_usage import service


@pytest.mark.parametrize('faults', [
    {'factory'}, {'add'}, {'execute'}, {'commit'}, {'close'},
    {'execute','rollback'}, {'execute','rollback','close'},
    {'execute','rollback','close','invalidate'}, {'close','invalidate'},
])
def test_record_usage_is_best_effort_through_all_session_phases(monkeypatch, faults):
    events, logged = [], []
    session = Mock()
    private = 'private-password-and-query'
    def operation(name):
        def perform(*args, **kwargs):
            events.append(name)
            if name in faults: raise RuntimeError(private)
        return perform
    for name in ['add','execute','commit','rollback','close','invalidate']:
        getattr(session, name).side_effect = operation(name)
    def factory():
        operation('factory')()
        return session
    monkeypatch.setattr(service, 'SessionLocal', factory)
    monkeypatch.setattr(service.logger, 'error', lambda message,*args: logged.append(message.format(*args)))
    assert service.record_usage(1, 'chat', prompt_tokens=2, completion_tokens=3, query_summary=private) is None
    assert logged and private not in str(logged)
    if 'factory' in faults:
        assert events == ['factory']
    else:
        session.close.assert_called_once()
        if faults.intersection({'add','execute','commit'}): session.rollback.assert_called_once()
        else: session.rollback.assert_not_called()
        assert events[-1] in {'close','invalidate'}
        if faults.intersection({'rollback','close'}): assert session.invalidate.called


def test_successful_recording_never_retries_after_close_failure(monkeypatch):
    session = Mock()
    session.close.side_effect = RuntimeError('private-connection-url')
    monkeypatch.setattr(service, 'SessionLocal', lambda: session)
    assert service.record_usage(1, 'chat', prompt_tokens=2, completion_tokens=3) is None
    session.commit.assert_called_once()
    session.execute.assert_called_once()
    session.rollback.assert_not_called()
    session.invalidate.assert_called_once()
