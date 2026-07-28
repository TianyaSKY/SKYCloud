"""分享 / 站内信 / 系统字典 / Token 用量测试。"""

from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

import pytest

from app.exceptions import BusinessRuleError, ResourceNotFoundError
from app.infra.datetime_utils import beijing_now
from app.models.user import User
from app.models.file import File
from app.models.folder import Folder
from app.models.share import Share
from app.models.inbox import Inbox
from app.models.sys_dict import SysDict
from app.models.token_usage_log import TokenUsageLog
from app.models.workspace import Workspace


# ---------------------------------------------------------------------------
# Share service
# ---------------------------------------------------------------------------


class TestShareService:
    def _create_file(self, session, test_workspace, test_user):
        f = File(
            name="test.pdf",
            file_path="test.pdf",
            file_size=100,
            workspace_id=test_workspace.id,
            uploader_id=test_user.id,
            status="success",
        )
        session.add(f)
        session.flush()
        return f

    def test_create_share_link(self, session, test_workspace, test_user):
        from app.features.share.service import create_share_link

        f = self._create_file(session, test_workspace, test_user)
        share = create_share_link(session, test_user.id, f.id)
        assert share.id is not None
        assert share.token is not None
        assert share.file_id == f.id

    def test_create_share_link_file_not_found(self, session, test_user):
        from app.features.share.service import create_share_link

        with pytest.raises(ValueError):
            create_share_link(session, test_user.id, 99999)

    def test_get_share_by_token_valid(self, session, test_workspace, test_user):
        from app.features.share.service import create_share_link, get_share_by_token

        f = self._create_file(session, test_workspace, test_user)
        share = create_share_link(session, test_user.id, f.id)
        found = get_share_by_token(session, share.token)
        assert found is not None
        assert found.id == share.id

    def test_get_share_by_token_expired(self, session, test_workspace, test_user):
        from app.features.share.service import get_share_by_token

        f = self._create_file(session, test_workspace, test_user)
        share = Share(
            user_id=test_user.id,
            file_id=f.id,
            expires_at=beijing_now() - timedelta(days=1),
        )
        session.add(share)
        session.flush()

        result = get_share_by_token(session, share.token)
        assert result is None

    def test_get_share_by_token_not_found(self, session):
        from app.features.share.service import get_share_by_token

        assert get_share_by_token(session, "nonexistent") is None

    def test_get_my_shares(self, session, test_workspace, test_user):
        from app.features.share.service import create_share_link, get_my_shares

        f = self._create_file(session, test_workspace, test_user)
        create_share_link(session, test_user.id, f.id)
        shares = get_my_shares(session, test_user.id)
        assert len(shares) >= 1

    def test_cancel_share(self, session, test_workspace, test_user):
        from app.features.share.service import create_share_link, cancel_share

        f = self._create_file(session, test_workspace, test_user)
        share = create_share_link(session, test_user.id, f.id)
        assert cancel_share(session, share.id, test_user.id) is True
        assert cancel_share(session, share.id, test_user.id) is False

    def test_create_share_with_expiry(self, session, test_workspace, test_user):
        from app.features.share.service import create_share

        f = self._create_file(session, test_workspace, test_user)
        future = (beijing_now() + timedelta(days=7)).isoformat()
        share = create_share(session, test_user.id, f.id, future)
        assert share.expires_at is not None

    def test_create_share_invalid_date(self, session, test_workspace, test_user):
        from app.features.share.service import create_share

        f = self._create_file(session, test_workspace, test_user)
        with pytest.raises(BusinessRuleError):
            create_share(session, test_user.id, f.id, "not-a-date")

    def test_cancel_share_for_user_not_found(self, session, test_user):
        from app.features.share.service import cancel_share_for_user

        with pytest.raises(ResourceNotFoundError):
            cancel_share_for_user(session, 99999, test_user.id)


# ---------------------------------------------------------------------------
# Inbox service
# ---------------------------------------------------------------------------


class TestInboxService:
    def test_create_message(self, session, test_user):
        from app.features.inbox.service import create_inbox_message

        msg = create_inbox_message(session, {
            "user_id": test_user.id,
            "title": "测试",
            "content": "内容",
            "type": "system",
        })
        assert msg.id is not None
        assert msg.is_read is False

    def test_get_user_inbox(self, session, test_user):
        from app.features.inbox.service import create_inbox_message, get_user_inbox

        create_inbox_message(session, {
            "user_id": test_user.id, "title": "m1", "content": "c1"
        })
        create_inbox_message(session, {
            "user_id": test_user.id, "title": "m2", "content": "c2"
        })
        result = get_user_inbox(session, test_user.id, page=1, per_page=10)
        assert result["total"] == 2
        assert len(result["items"]) == 2
        assert result["pages"] == 1

    def test_get_user_inbox_pagination(self, session, test_user):
        from app.features.inbox.service import create_inbox_message, get_user_inbox

        for i in range(5):
            create_inbox_message(session, {
                "user_id": test_user.id, "title": f"m{i}", "content": f"c{i}"
            })
        result = get_user_inbox(session, test_user.id, page=1, per_page=2)
        assert result["total"] == 5
        assert len(result["items"]) == 2
        assert result["pages"] == 3

    def test_get_inbox_message(self, session, test_user):
        from app.features.inbox.service import create_inbox_message, get_inbox_message

        msg = create_inbox_message(session, {
            "user_id": test_user.id, "title": "t", "content": "c"
        })
        found = get_inbox_message(session, msg.id)
        assert found.id == msg.id

    def test_get_inbox_message_not_found(self, session):
        from app.features.inbox.service import get_inbox_message

        with pytest.raises(ResourceNotFoundError):
            get_inbox_message(session, 99999)

    def test_mark_as_read(self, session, test_user):
        from app.features.inbox.service import create_inbox_message, mark_as_read

        msg = create_inbox_message(session, {
            "user_id": test_user.id, "title": "t", "content": "c"
        })
        updated = mark_as_read(session, msg.id, test_user.id)
        assert updated.is_read is True

    def test_mark_as_read_wrong_user(self, session, test_user):
        from app.features.inbox.service import create_inbox_message, mark_as_read

        msg = create_inbox_message(session, {
            "user_id": test_user.id, "title": "t", "content": "c"
        })
        with pytest.raises(ResourceNotFoundError):
            mark_as_read(session, msg.id, 99999)

    def test_delete_inbox_message(self, session, test_user):
        from app.features.inbox.service import create_inbox_message, delete_inbox_message, get_user_inbox

        msg = create_inbox_message(session, {
            "user_id": test_user.id, "title": "t", "content": "c"
        })
        delete_inbox_message(session, msg.id, test_user.id)
        result = get_user_inbox(session, test_user.id)
        assert result["total"] == 0

    def test_mark_all_as_read(self, session, test_user):
        from app.features.inbox.service import create_inbox_message, mark_all_as_read, get_user_inbox

        for i in range(3):
            create_inbox_message(session, {
                "user_id": test_user.id, "title": f"m{i}", "content": f"c{i}"
            })
        mark_all_as_read(session, test_user.id)
        result = get_user_inbox(session, test_user.id)
        for item in result["items"]:
            assert item.is_read is True


# ---------------------------------------------------------------------------
# SysDict service
# ---------------------------------------------------------------------------


class TestSysDictService:
    def test_create_sys_dict(self, session, mock_redis):
        from app.features.sys_dict import service as sys_dict_svc

        with patch.object(sys_dict_svc, "db") as mock_db:
            mock_db.session = session
            result = sys_dict_svc.create_sys_dict({
                "key": "test_key",
                "value": "test_value",
                "des": "描述",
                "enable": True,
            })
            assert result.key == "test_key"

    def test_create_model_config_key_rejected(self, session):
        from app.features.sys_dict import service as sys_dict_svc

        with patch.object(sys_dict_svc, "db") as mock_db:
            mock_db.session = session
            with pytest.raises(BusinessRuleError):
                sys_dict_svc.create_sys_dict({
                    "key": "chat_api_url",
                    "value": "http://x",
                    "des": "",
                    "enable": True,
                })

    def test_update_sys_dict(self, session, mock_redis):
        from app.features.sys_dict import service as sys_dict_svc

        sd = SysDict(key="upd_key", value="old", des="d", enable=True)
        session.add(sd)
        session.flush()

        with patch.object(sys_dict_svc, "db") as mock_db:
            mock_db.session = session
            updated = sys_dict_svc.update_sys_dict(sd.id, {"value": "new"})
            assert updated.value == "new"

    def test_update_nonexistent(self, session):
        from app.features.sys_dict import service as sys_dict_svc

        with patch.object(sys_dict_svc, "db") as mock_db:
            mock_db.session = session
            with pytest.raises(ResourceNotFoundError):
                sys_dict_svc.update_sys_dict(99999, {"value": "x"})

    def test_delete_sys_dict(self, session, mock_redis):
        from app.features.sys_dict import service as sys_dict_svc

        sd = SysDict(key="del_key", value="v", des="", enable=True)
        session.add(sd)
        session.flush()

        with patch.object(sys_dict_svc, "db") as mock_db:
            mock_db.session = session
            sys_dict_svc.delete_sys_dict(sd.id)
            assert session.get(SysDict, sd.id) is None

    def test_delete_nonexistent(self, session):
        from app.features.sys_dict import service as sys_dict_svc

        with patch.object(sys_dict_svc, "db") as mock_db:
            mock_db.session = session
            with pytest.raises(ResourceNotFoundError):
                sys_dict_svc.delete_sys_dict(99999)

    def test_get_sys_dict_by_key_sync(self, session):
        from app.features.sys_dict import service as sys_dict_svc

        sd = SysDict(key="sync_key", value="sv", des="", enable=True)
        session.add(sd)
        session.flush()

        with patch.object(sys_dict_svc, "db") as mock_db:
            mock_db.session = session
            result = sys_dict_svc.get_sys_dict_by_key_sync("sync_key")
            assert result is not None
            assert result.value == "sv"

    def test_get_sys_dict_by_key_sync_model_config(self, session):
        from app.features.sys_dict import service as sys_dict_svc

        result = sys_dict_svc.get_sys_dict_by_key_sync("chat_api_key")
        assert result is None


# ---------------------------------------------------------------------------
# Token usage service
# ---------------------------------------------------------------------------


class TestTokenUsageService:
    def test_get_user_token_stats(self, session, test_user):
        from app.features.token_usage.service import get_user_token_stats

        test_user.total_prompt_tokens = 100
        test_user.total_completion_tokens = 50
        test_user.total_tokens = 150
        session.flush()

        stats = get_user_token_stats(session, test_user.id)
        assert stats["user_id"] == test_user.id
        assert stats["total_prompt_tokens"] == 100
        assert stats["total_tokens"] == 150

    def test_get_user_token_stats_nonexistent(self, session):
        from app.features.token_usage.service import get_user_token_stats

        assert get_user_token_stats(session, 99999) == {}

    def test_get_usage_logs(self, session, test_user):
        from app.features.token_usage.service import get_usage_logs

        log = TokenUsageLog(
            user_id=test_user.id,
            action="chat",
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
        )
        session.add(log)
        session.flush()

        result = get_usage_logs(session, test_user.id)
        assert result["total"] == 1
        assert result["items"][0]["action"] == "chat"

    def test_get_usage_logs_filter_action(self, session, test_user):
        from app.features.token_usage.service import get_usage_logs

        session.add(TokenUsageLog(user_id=test_user.id, action="chat", total_tokens=10))
        session.add(TokenUsageLog(user_id=test_user.id, action="embedding", total_tokens=5))
        session.flush()

        result = get_usage_logs(session, test_user.id, action="chat")
        assert result["total"] == 1

    def test_get_daily_stats(self, session, test_user):
        from app.features.token_usage.service import get_daily_stats

        log = TokenUsageLog(
            user_id=test_user.id,
            action="chat",
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            created_at=beijing_now(),
        )
        session.add(log)
        session.flush()

        stats = get_daily_stats(session, test_user.id, days=30)
        assert len(stats) >= 1
        assert stats[0]["total_tokens"] == 15

    def test_get_all_users_token_stats(self, session, test_user, admin_user):
        from app.features.token_usage.service import get_all_users_token_stats

        test_user.total_tokens = 100
        admin_user.total_tokens = 200
        session.flush()

        stats = get_all_users_token_stats(session)
        assert len(stats) >= 2
        # 按 total_tokens 降序
        assert stats[0]["total_tokens"] >= stats[1]["total_tokens"]

    def test_get_all_users_usage_logs(self, session, test_user):
        from app.features.token_usage.service import get_all_users_usage_logs

        session.add(TokenUsageLog(user_id=test_user.id, action="chat", total_tokens=10))
        session.flush()

        result = get_all_users_usage_logs(session)
        assert result["total"] >= 1
        assert "username" in result["items"][0]

    def test_get_all_users_daily_stats(self, session, test_user):
        from app.features.token_usage.service import get_all_users_daily_stats

        session.add(TokenUsageLog(
            user_id=test_user.id, action="chat",
            prompt_tokens=5, completion_tokens=3, total_tokens=8,
            created_at=beijing_now(),
        ))
        session.flush()

        stats = get_all_users_daily_stats(session, days=30)
        assert len(stats) >= 1

    def test_get_per_user_daily_stats(self, session, test_user):
        from app.features.token_usage.service import get_per_user_daily_stats

        session.add(TokenUsageLog(
            user_id=test_user.id, action="chat",
            prompt_tokens=5, completion_tokens=3, total_tokens=8,
            created_at=beijing_now(),
        ))
        session.flush()

        stats = get_per_user_daily_stats(session, days=30)
        assert len(stats) >= 1
        assert stats[0]["username"] == "testuser"
