"""Preserve absolute instants across the legacy Beijing-naive SQL boundary."""
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.features.share import service as shares
from app.features.token_usage import service as usage
from app.models.file import File
from app.models.token_usage_log import TokenUsageLog


def assert_share_and_usage_times(session, user_id, workspace_id):
    file = File(name="clock.txt", file_path="synthetic-clock.txt", file_size=1,
                workspace_id=workspace_id, uploader_id=user_id, status="success")
    session.add(file)
    session.flush()
    share = shares.create_share(session, user_id, file.id, "2025-06-01T04:30:00Z")
    session.refresh(share)
    assert share.expires_at == datetime(2025, 6, 1, 4, 30, tzinfo=timezone.utc)
    assert share.to_dict()["expires_at"] == "2025-06-01T04:30:00+00:00"
    with patch.object(shares, "utc_now", return_value=datetime(2025, 6, 1, 4, tzinfo=timezone.utc)):
        assert shares.get_share_by_token(session, share.token) is not None
    with patch.object(shares, "utc_now", return_value=datetime(2025, 6, 1, 5, tzinfo=timezone.utc)):
        assert shares.get_share_by_token(session, share.token) is None
    with patch.object(shares, "utc_now", return_value=datetime(2025, 6, 1, 4, 30, tzinfo=timezone.utc)):
        assert shares.get_share_by_token(session, share.token) is None

    session.add(TokenUsageLog(user_id=user_id, action="chat", total_tokens=5,
                             created_at=datetime(2025, 6, 1, 12, 15)))
    session.commit()
    for query in (usage.get_usage_logs, usage.get_all_users_usage_logs):
        result = query(session, user_id=user_id,
            start_date="2025-06-01T00:00:00-04:00", end_date="2025-06-01T05:00:00Z")
        assert result["total"] == 1
        assert result["items"][0]["created_at"] == "2025-06-01T04:15:00+00:00"
        assert query(session, user_id=user_id, start_date="2025-06-01T04:20:00Z")["total"] == 0


def test_share_expiration_and_usage_ranges_preserve_instants(session, test_user, test_workspace):
    assert_share_and_usage_times(session, test_user.id, test_workspace.id)


@pytest.mark.parametrize("value", [datetime(2025, 6, 1, 0), datetime(2025, 1, 1, 0)])
def test_serialized_user_cache_roundtrip_keeps_midnight_instant(value):
    from app.models.user import User
    user = User(id=1, username="clock", role="common", created_at=value)
    cached = user.to_dict()
    restored = User.from_cache(cached)
    from app.infra.datetime_utils import to_utc_datetime
    assert restored.created_at == to_utc_datetime(value)
    assert restored.to_dict()["created_at"] == cached["created_at"]
    assert cached["created_at"].endswith("+00:00")


@pytest.mark.parametrize("model_name", ["user", "file", "folder", "share", "inbox", "sys_dict"])
@pytest.mark.parametrize("cached_time", ["2025-06-01T00:00:00", "2025-05-31T16:00:00Z"])
def test_model_cache_accepts_legacy_and_utc_midnight(model_name, cached_time):
    import importlib
    names = {"user": "User", "file": "File", "folder": "Folder", "share": "Share",
             "inbox": "Inbox", "sys_dict": "SysDict"}
    model = getattr(importlib.import_module("app.models." + model_name), names[model_name])
    restored = model.from_cache({"id": 1, "created_at": cached_time})
    assert restored.created_at == datetime(2025, 5, 31, 16, tzinfo=timezone.utc)


def test_legacy_reservation_naive_inputs_are_utc(session, test_user, test_workspace):
    from app.models.upload_reservation import UploadReservation
    row = UploadReservation(id="utc-input", user_id=test_user.id,
        workspace_id=test_workspace.id, size=1, kind="file", status="active",
        created_at=datetime(2025, 6, 1, 4), expires_at=datetime(2025, 6, 1, 5))
    session.add(row)
    session.commit()
    session.refresh(row)
    assert row.created_at == datetime(2025, 6, 1, 4, tzinfo=timezone.utc)
    assert row.expires_at == datetime(2025, 6, 1, 5, tzinfo=timezone.utc)
