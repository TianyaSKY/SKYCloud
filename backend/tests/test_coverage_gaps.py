"""补充覆盖率缺口：token_usage admin / extensions / change_log / folder service。"""
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from app.features.token_usage.service import (
    record_usage,
    get_usage_logs,
    get_daily_stats,
    get_all_users_token_stats,
    get_all_users_usage_logs,
    get_all_users_daily_stats,
    get_per_user_daily_stats,
)
from app.models.token_usage_log import TokenUsageLog


# ---------------------------------------------------------------------------
# token_usage admin 接口
# ---------------------------------------------------------------------------
class TestTokenUsageAdmin:
    def test_record_usage_exception(self):
        """record_usage 写库异常应被吞掉只记日志。"""
        mock_session = MagicMock()
        mock_session.commit.side_effect = Exception("DB down")
        with patch("app.features.token_usage.service.SessionLocal", return_value=mock_session), \
             patch("app.features.token_usage.service.logger"):
            record_usage(user_id=1, action="chat", prompt_tokens=5, completion_tokens=10)
        mock_session.rollback.assert_called_once()
        mock_session.close.assert_called_once()

    def test_get_usage_logs_date_filters(self, session, test_user):
        """有效/无效日期过滤分支。"""
        session.add(TokenUsageLog(
            user_id=test_user.id, action="chat", model_name="m",
            prompt_tokens=10, completion_tokens=20, total_tokens=30,
        ))
        session.flush()
        # 有效日期
        result = get_usage_logs(
            session, test_user.id,
            start_date="2020-01-01T00:00:00",
            end_date="2099-12-31T00:00:00",
        )
        assert result["total"] >= 1
        # 无效日期（应忽略 ValueError 分支）
        result2 = get_usage_logs(
            session, test_user.id,
            start_date="not-a-date",
            end_date="also-bad",
        )
        assert result2["total"] >= 1

    def test_get_daily_stats_empty(self, session, test_user):
        """无数据时返回空列表。"""
        result = get_daily_stats(session, test_user.id, days=1)
        assert isinstance(result, list)

    def test_get_all_users_token_stats(self, session, test_user):
        result = get_all_users_token_stats(session)
        assert isinstance(result, list)
        assert any(u["user_id"] == test_user.id for u in result)

    def test_get_all_users_usage_logs_filters(self, session, test_user):
        """admin 明细查询：user_id/action/有效日期/无效日期分支。"""
        session.add(TokenUsageLog(
            user_id=test_user.id, action="chat", model_name="m",
            prompt_tokens=1, completion_tokens=2, total_tokens=3,
        ))
        session.flush()
        # user_id 过滤
        r1 = get_all_users_usage_logs(session, user_id=test_user.id)
        assert r1["total"] >= 1
        # action 过滤
        r2 = get_all_users_usage_logs(session, action="chat")
        assert r2["total"] >= 1
        # 有效日期
        r3 = get_all_users_usage_logs(
            session,
            start_date="2020-01-01T00:00:00",
            end_date="2099-12-31T00:00:00",
        )
        assert r3["total"] >= 1
        # 无效日期
        r4 = get_all_users_usage_logs(
            session,
            start_date="bad",
            end_date="bad2",
        )
        assert r4["total"] >= 1

    def test_get_all_users_daily_stats(self, session, test_user):
        session.add(TokenUsageLog(
            user_id=test_user.id, action="chat", model_name="m",
            prompt_tokens=1, completion_tokens=2, total_tokens=3,
        ))
        session.flush()
        result = get_all_users_daily_stats(session, days=30)
        assert isinstance(result, list)

    def test_get_per_user_daily_stats(self, session, test_user):
        session.add(TokenUsageLog(
            user_id=test_user.id, action="chat", model_name="m",
            prompt_tokens=1, completion_tokens=2, total_tokens=3,
        ))
        session.flush()
        result = get_per_user_daily_stats(session, days=30)
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# infra/extensions.py
# ---------------------------------------------------------------------------
class TestExtensions:
    def test_get_db_yields_session(self):
        from app.infra.extensions import get_db
        gen = get_db()
        session = next(gen)
        assert session is not None
        try:
            next(gen)
        except StopIteration:
            pass

    def test_db_proxy_session(self):
        from app.infra.extensions import db, ScopedSession
        s = db.session
        assert s is not None
        ScopedSession.remove()

    def test_constants(self):
        from app.infra.extensions import UPLOAD_FOLDER
        # 只验证常量可访问
        assert UPLOAD_FOLDER is not None


# ---------------------------------------------------------------------------
# folder/change_log.py
# ---------------------------------------------------------------------------
class TestChangeLog:
    def test_build_folder_path_root(self):
        from app.features.folder.change_log import _build_folder_path
        assert _build_folder_path(None, {}) == "根目录"
        assert _build_folder_path(0, {}) == "根目录"

    def test_build_folder_path_chain(self):
        from app.features.folder.change_log import _build_folder_path
        f1 = MagicMock(); f1.name = "a"; f1.parent_id = None
        f2 = MagicMock(); f2.name = "b"; f2.parent_id = 1
        folder_map = {1: f1, 2: f2}
        assert _build_folder_path(2, folder_map) == "根目录/a/b"

    def test_build_folder_path_cycle(self):
        from app.features.folder.change_log import _build_folder_path
        f1 = MagicMock(); f1.name = "a"; f1.parent_id = 2
        f2 = MagicMock(); f2.name = "b"; f2.parent_id = 1
        folder_map = {1: f1, 2: f2}
        # 带环检测，不死循环
        path = _build_folder_path(1, folder_map)
        assert "a" in path

    def test_build_folder_path_not_in_map(self):
        from app.features.folder.change_log import _build_folder_path
        assert _build_folder_path(999, {}) == "根目录"

    def test_to_payload_text(self):
        from app.features.folder.change_log import _to_payload_text
        assert _to_payload_text(None) is None
        assert _to_payload_text("str") == "str"
        result = _to_payload_text({"k": "v"})
        assert "v" in result

    def test_log_event_success(self, session, test_workspace):
        from app.features.folder.change_log import log_event
        with patch("app.features.folder.change_log.SessionLocal", return_value=session):
            result = log_event(
                workspace_id=test_workspace.id,
                entity_type="file",
                entity_id=1,
                action="create",
                payload={"k": "v"},
            )
        assert result is True

    def test_log_event_skip_none_entity(self, session, test_workspace):
        from app.features.folder.change_log import log_events_batch
        with patch("app.features.folder.change_log.SessionLocal", return_value=session):
            count = log_events_batch(test_workspace.id, [{"entity_id": None}])
        assert count == 0

    def test_log_events_batch_empty(self, session, test_workspace):
        from app.features.folder.change_log import log_events_batch
        with patch("app.features.folder.change_log.SessionLocal", return_value=session):
            count = log_events_batch(test_workspace.id, [])
        assert count == 0

    def test_log_events_batch_exception(self, session, test_workspace):
        from app.features.folder.change_log import log_events_batch
        mock_session = MagicMock()
        mock_session.commit.side_effect = Exception("DB fail")
        with patch("app.features.folder.change_log.SessionLocal", return_value=mock_session), \
             patch("app.features.folder.change_log.logger"):
            count = log_events_batch(test_workspace.id, [{"entity_id": 1}])
        assert count == 0

    def test_get_latest_event_id_none(self, session, test_workspace):
        from app.features.folder.change_log import get_latest_event_id
        assert get_latest_event_id(session, test_workspace.id) == 0

    def test_get_checkpoint_event_id_none(self, session, test_workspace):
        from app.features.folder.change_log import get_checkpoint_event_id
        assert get_checkpoint_event_id(session, test_workspace.id) == 0

    def test_update_checkpoint_create(self, session, test_workspace):
        from app.features.folder.change_log import update_checkpoint, get_checkpoint_event_id
        update_checkpoint(session, test_workspace.id, 5, mark_full_scan=True)
        assert get_checkpoint_event_id(session, test_workspace.id) == 5

    def test_update_checkpoint_update_only_grows(self, session, test_workspace):
        from app.features.folder.change_log import update_checkpoint, get_checkpoint_event_id
        update_checkpoint(session, test_workspace.id, 10)
        assert get_checkpoint_event_id(session, test_workspace.id) == 10
        # 较小值不应回退
        update_checkpoint(session, test_workspace.id, 3)
        assert get_checkpoint_event_id(session, test_workspace.id) == 10

    def test_update_checkpoint_exception(self, session, test_workspace):
        from app.features.folder.change_log import update_checkpoint
        mock_session = MagicMock()
        mock_session.commit.side_effect = Exception("DB fail")
        with patch.object(session, "commit", side_effect=Exception("DB fail")), \
             patch("app.features.folder.change_log.logger"):
            update_checkpoint(session, test_workspace.id, 5)
        # 不抛异常

    def test_load_incremental_context_no_changes(self, session, test_workspace):
        from app.features.folder.change_log import load_incremental_context
        result = load_incremental_context(session, test_workspace.id)
        assert result["has_changes"] is False

    def test_load_incremental_context_with_events(self, session, test_workspace):
        from app.features.folder.change_log import load_incremental_context
        from app.models.file_change_event import FileChangeEvent
        # 直接在测试 session 内插入事件
        session.add(FileChangeEvent(
            workspace_id=test_workspace.id, entity_type="file",
            entity_id=1, action="create",
        ))
        session.add(FileChangeEvent(
            workspace_id=test_workspace.id, entity_type="folder",
            entity_id=2, action="update_meta",
        ))
        session.flush()
        result = load_incremental_context(session, test_workspace.id)
        assert result["has_changes"] is True
        assert result["total_events"] == 2

    def test_resolve_changed_details(self, session, test_workspace):
        from app.features.folder.change_log import _resolve_changed_details
        from app.models.folder import Folder
        f = Folder(name="d1", workspace_id=test_workspace.id, parent_id=None)
        session.add(f)
        session.flush()
        result = _resolve_changed_details(
            session, test_workspace.id,
            changed_file_ids=[],
            changed_folder_ids=[0, f.id, 999],
        )
        assert len(result["changed_folders_detail"]) == 2
        assert result["changed_folders_detail"][0]["name"] == "根目录"


# ---------------------------------------------------------------------------
# folder/service.py 剩余 Redis 锁逻辑
# ---------------------------------------------------------------------------
class TestFolderServiceLocks:
    def test_organize_task_lock_key(self):
        from app.features.folder.service import _organize_task_lock_key
        assert _organize_task_lock_key(5) == "organize:task:lock:5"

    def test_invalidate_folder_caches(self):
        from app.features.folder.service import _invalidate_folder_caches
        with patch("app.features.folder.service.evict_cache") as mock_evict:
            _invalidate_folder_caches(1)
        assert mock_evict.call_count == 3

    def test_mark_organize_task_running(self):
        from app.features.folder.service import mark_organize_task_running
        with patch("app.features.folder.service.redis_client") as mock_redis:
            mark_organize_task_running(1, "token-abc")
        mock_redis.eval.assert_called_once()

    def test_release_organize_task_lock(self):
        from app.features.folder.service import release_organize_task_lock
        with patch("app.features.folder.service.redis_client") as mock_redis:
            release_organize_task_lock(1, "token-abc")
        mock_redis.eval.assert_called_once()

    def test_organize_files_no_lock(self):
        """Redis 锁竞争失败时应返回 False。"""
        from app.features.folder.service import organize_files
        mock_redis = MagicMock()
        # set nx 返回 None 表示未获取锁
        mock_redis.set.return_value = None
        with patch("app.features.folder.service.redis_client", mock_redis):
            result = organize_files(1, 1)
        assert result is False

    def test_organize_files_success(self):
        """获取锁后应入队并返回 True。"""
        from app.features.folder.service import organize_files
        mock_redis = MagicMock()
        mock_redis.set.return_value = True  # 获取锁成功
        with patch("app.features.folder.service.redis_client", mock_redis), \
             patch("app.features.folder.service.publish_organize_task") as mock_pub:
            result = organize_files(1, 1)
        assert result is True
        mock_pub.assert_called_once()

    def test_organize_files_publish_fail(self):
        """入队失败应释放锁并抛异常。"""
        from app.features.folder.service import organize_files
        mock_redis = MagicMock()
        mock_redis.set.return_value = True
        with patch("app.features.folder.service.redis_client", mock_redis), \
             patch("app.features.folder.service.publish_organize_task",
                   side_effect=Exception("MQ down")), \
             patch("app.features.folder.service.release_organize_task_lock") as mock_rel:
            with pytest.raises(Exception):
                organize_files(1, 1)
        mock_rel.assert_called_once()


# ---------------------------------------------------------------------------
# infra/cache.py 剩余分支
# ---------------------------------------------------------------------------
class TestCacheRemaining:
    def test_cacheable_prefix_attribute(self):
        from app.infra.cache import cacheable

        @cacheable(prefix="myfunc", expire=60)
        def fn(x):
            return x

        assert fn.cache_prefix == "myfunc"

    def test_cacheable_async_prefix_attribute(self):
        from app.infra.cache import cacheable

        @cacheable(prefix="asyncfunc", expire=60)
        async def fn(x):
            return x

        assert fn.cache_prefix == "asyncfunc"

    def test_cacheable_returns_none_default(self):
        """cache_none=False 且函数返回 None 时跳过缓存写入。"""
        from app.infra.cache import cacheable
        mock_redis = MagicMock()
        mock_redis.get.return_value = None

        @cacheable(prefix="test", expire=60)
        def fn():
            return None

        with patch("app.infra.cache.redis_client", mock_redis):
            result = fn()
        assert result is None
        mock_redis.setex.assert_not_called()

    def test_cache_evict_before_async(self):
        from app.infra.cache import cache_evict
        mock_redis = MagicMock()

        @cache_evict(prefix="test", before_invocation=True)
        async def fn(x):
            return x

        with patch("app.infra.cache.redis_client", mock_redis):
            result = __import__("asyncio").run(fn(1))
        assert result == 1
        mock_redis.delete.assert_called_once()

    def test_cache_put_async_none(self):
        from app.infra.cache import cache_put
        mock_redis = MagicMock()

        @cache_put(prefix="test", expire=60)
        async def fn():
            return None

        with patch("app.infra.cache.redis_client", mock_redis):
            result = __import__("asyncio").run(fn())
        assert result is None
        mock_redis.setex.assert_not_called()

    def test_cache_evict_all_entries_async(self):
        from app.infra.cache import cache_evict
        mock_redis = MagicMock()
        mock_redis.scan_iter.return_value = iter(["t:1"])

        @cache_evict(prefix="t", all_entries=True)
        async def fn():
            return "done"

        with patch("app.infra.cache.redis_client", mock_redis):
            result = __import__("asyncio").run(fn())
        assert result == "done"
