"""Cleanup faults preserve caller errors and never log private SQL details."""
from unittest.mock import MagicMock
import pytest
from loguru import logger
from sqlalchemy.exc import StatementError
from app.features.file import upload_recovery


@pytest.mark.parametrize('fault', ['none','rollback','invalidation','compensation'])
def test_recovery_resets_or_defers_without_abandoning_healthy_entries(monkeypatch, fault):
    private = 'private-recovery-sql-sentinel'
    error = StatementError(private, 'SELECT :password', {'password':private}, ValueError(private))
    db = MagicMock()
    release = MagicMock()
    if fault in ['rollback','invalidation']:
        db.rollback.side_effect = error
    if fault == 'invalidation':
        db.invalidate.side_effect = error
    if fault == 'compensation':
        release.side_effect = [error,None]
    monkeypatch.setattr(upload_recovery, '_release_failed_upload', release)
    records = []
    sink = logger.add(lambda message: records.append(message.record))
    try:
        upload_recovery.compensate_failed_uploads(db, 1, 2, [('one','object-one'),('two','object-two')], None)
    finally:
        logger.remove(sink)
    assert release.call_count == (0 if fault == 'invalidation' else 2)
    assert db.rollback.call_count == (2 if fault == 'compensation' else 1)
    assert db.invalidate.call_count == int(fault in ['rollback','invalidation'])
    assert all(private not in r['message'] and r['exception'] is None for r in records)
