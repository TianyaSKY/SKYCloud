"""补充覆盖率缺口：token_usage admin / extensions / change_log / folder service。"""
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.features.folder.change_log import FileChangeEventSpec
from app.features.token_usage.service import (
    AdminUsageLogsParams,
    GetUsageLogsParams,
    RecordUsageParams,
    record_usage,
    get_usage_logs,
    get_daily_stats,
    get_all_users_token_stats,
    get_all_users_usage_logs,
    get_all_users_daily_stats,
    get_per_user_daily_stats,
)
from app.infra.cache import CacheableParams
from app.models.folder import Folder
from app.models.token_usage_log import TokenUsageLog
from app.models.user import User
from app.models.workspace import Workspace


# token_usage admin 接口
class TestTokenUsageAdmin:
    def test_record_usage_exception(self) -> None:
        """record_usage 写库异常应被吞掉只记日志。"""
        mock_session = MagicMock()
        mock_session.commit.side_effect = Exception("DB down")
        with patch("app.features.token_usage.service.SessionLocal", return_value=mock_session), \
             patch("app.features.token_usage.service.logger"):
            record_usage(RecordUsageParams(user_id=1, action="chat", prompt_tokens=5, completion_tokens=10))
        mock_session.rollback.assert_called_once()
        mock_session.close.assert_called_once()

    def test_get_usage_logs_date_filters(self, session: Session, test_user: User) -> None:
        """有效/无效日期过滤分支。"""
        session.add(TokenUsageLog(
            user_id=test_user.id, action="chat", model_name="m",
            prompt_tokens=10, completion_tokens=20, total_tokens=30,
        ))
        session.flush()
        # 有效日期
        result = get_usage_logs(session, GetUsageLogsParams(user_id=test_user.id, start_date="2020-01-01T00:00:00",
        end_date="2099-12-31T00:00:00",))
        assert result["total"] >= 1
        from app.exceptions import BusinessRuleError
        with pytest.raises(BusinessRuleError):
            get_usage_logs(session, GetUsageLogsParams(user_id=test_user.id, start_date="bad", end_date="bad2"))

    def test_get_daily_stats_empty(self, session: Session, test_user: User) -> None:
        """无数据时返回空列表。"""
        result = get_daily_stats(session, test_user.id, days=1)
        assert isinstance(result, list)

    def test_get_all_users_token_stats(self, session: Session, test_user: User) -> None:
        result = get_all_users_token_stats(session)
        assert isinstance(result, list)
        assert any(cast(dict[str, Any], u)["user_id"] == test_user.id for u in result)

    def test_get_all_users_usage_logs_filters(self, session: Session, test_user: User) -> None:
        """admin 明细查询：user_id/action/有效日期/无效日期分支。"""
        session.add(TokenUsageLog(
            user_id=test_user.id, action="chat", model_name="m",
            prompt_tokens=1, completion_tokens=2, total_tokens=3,
        ))
        session.flush()
        # user_id 过滤
        r1 = get_all_users_usage_logs(session, AdminUsageLogsParams(user_id=test_user.id))
        assert r1["total"] >= 1
        # action 过滤
        r2 = get_all_users_usage_logs(session, AdminUsageLogsParams(action="chat"))
        assert r2["total"] >= 1
        # 有效日期
        r3 = get_all_users_usage_logs(session, AdminUsageLogsParams(start_date="2020-01-01T00:00:00",
        end_date="2099-12-31T00:00:00",))
        assert r3["total"] >= 1
        from app.exceptions import BusinessRuleError
        with pytest.raises(BusinessRuleError):
            get_all_users_usage_logs(session, AdminUsageLogsParams(start_date="bad", end_date="bad2"))

    def test_get_all_users_daily_stats(self, session: Session, test_user: User) -> None:
        session.add(TokenUsageLog(
            user_id=test_user.id, action="chat", model_name="m",
            prompt_tokens=1, completion_tokens=2, total_tokens=3,
        ))
        session.flush()
        result = get_all_users_daily_stats(session, days=30)
        assert isinstance(result, list)

    def test_get_per_user_daily_stats(self, session: Session, test_user: User) -> None:
        session.add(TokenUsageLog(
            user_id=test_user.id, action="chat", model_name="m",
            prompt_tokens=1, completion_tokens=2, total_tokens=3,
        ))
        session.flush()
        result = get_per_user_daily_stats(session, days=30)
        assert isinstance(result, list)


# infra/extensions.py
class TestExtensions:
    def test_get_db_yields_session(self) -> None:
        from app.infra.extensions import get_db
        gen = get_db()
        session = next(gen)
        assert session is not None
        try:
            next(gen)
        except StopIteration:
            pass

    def test_db_proxy_session(self) -> None:
        from app.infra.extensions import db, ScopedSession
        s = db.session
        assert s is not None
        ScopedSession.remove()

    def test_constants(self) -> None:
        from app.infra.extensions import UPLOAD_FOLDER
        # 只验证常量可访问
        assert UPLOAD_FOLDER is not None


# folder/change_log.py
class TestChangeLog:
    def test_build_folder_path_root(self) -> None:
        from app.features.folder.change_log import _build_folder_path  # pyright: ignore[reportPrivateUsage]
        assert _build_folder_path(None, {}) == "根目录"
        assert _build_folder_path(0, {}) == "根目录"

    def test_build_folder_path_chain(self) -> None:
        from app.features.folder.change_log import _build_folder_path  # pyright: ignore[reportPrivateUsage]
        f1 = Folder(id=1, name="a", parent_id=None)
        f2 = Folder(id=2, name="b", parent_id=1)
        folder_map = {1: f1, 2: f2}
        assert _build_folder_path(2, folder_map) == "根目录/a/b"

    def test_build_folder_path_cycle(self) -> None:
        from app.features.folder.change_log import _build_folder_path  # pyright: ignore[reportPrivateUsage]
        f1 = Folder(id=1, name="a", parent_id=2)
        f2 = Folder(id=2, name="b", parent_id=1)
        folder_map = {1: f1, 2: f2}
        # 带环检测，不死循环
        path = _build_folder_path(1, folder_map)
        assert "a" in path

    def test_build_folder_path_not_in_map(self) -> None:
        from app.features.folder.change_log import _build_folder_path  # pyright: ignore[reportPrivateUsage]
        assert _build_folder_path(999, {}) == "根目录"

    def test_to_payload_text(self) -> None:
        from app.features.folder.change_log import _to_payload_text  # pyright: ignore[reportPrivateUsage]
        assert _to_payload_text(None) is None
        assert _to_payload_text("str") == "str"
        result = _to_payload_text({"k": "v"})
        assert result is not None and "v" in result

    def test_log_event_success(self, session: Session, test_workspace: Workspace) -> None:
        from app.features.folder.change_log import LogEventParams, log_event
        with patch("app.features.folder.change_log.SessionLocal", return_value=session):
            result = log_event(
                LogEventParams(
                    workspace_id=test_workspace.id,
                    entity_type="file",
                    entity_id=1,
                    action="create",
                    payload={"k": "v"},
                ),
            )
        assert result is True

    def test_log_event_skip_none_entity(self, session: Session, test_workspace: Workspace) -> None:
        from app.features.folder.change_log import log_events_batch
        with patch("app.features.folder.change_log.SessionLocal", return_value=session):
            count = log_events_batch(test_workspace.id, [FileChangeEventSpec(entity_type="file", entity_id=None, action="create")])
        assert count == 0

    def test_log_events_batch_empty(self, session: Session, test_workspace: Workspace) -> None:
        from app.features.folder.change_log import log_events_batch
        with patch("app.features.folder.change_log.SessionLocal", return_value=session):
            count = log_events_batch(test_workspace.id, [])
        assert count == 0

    def test_log_events_batch_exception(self, session: Session, test_workspace: Workspace) -> None:
        from app.features.folder.change_log import log_events_batch
        mock_session = MagicMock()
        mock_session.commit.side_effect = Exception("DB fail")
        with patch("app.features.folder.change_log.SessionLocal", return_value=mock_session), \
             patch("app.features.folder.change_log.logger"):
            count = log_events_batch(test_workspace.id, [FileChangeEventSpec(entity_type="file", entity_id=1, action="create")])
        assert count == 0

    def test_get_latest_event_id_none(self, session: Session, test_workspace: Workspace) -> None:
        from app.features.folder.change_log import get_latest_event_id
        assert get_latest_event_id(session, test_workspace.id) == 0

    def test_get_checkpoint_event_id_none(self, session: Session, test_workspace: Workspace) -> None:
        from app.features.folder.change_log import get_checkpoint_event_id
        assert get_checkpoint_event_id(session, test_workspace.id) == 0

    def test_update_checkpoint_create(self, session: Session, test_workspace: Workspace) -> None:
        from app.features.folder.change_log import update_checkpoint, get_checkpoint_event_id
        update_checkpoint(session, test_workspace.id, 5, mark_full_scan=True)
        assert get_checkpoint_event_id(session, test_workspace.id) == 5

    def test_update_checkpoint_update_only_grows(self, session: Session, test_workspace: Workspace) -> None:
        from app.features.folder.change_log import update_checkpoint, get_checkpoint_event_id
        update_checkpoint(session, test_workspace.id, 10)
        assert get_checkpoint_event_id(session, test_workspace.id) == 10
        # 较小值不应回退
        update_checkpoint(session, test_workspace.id, 3)
        assert get_checkpoint_event_id(session, test_workspace.id) == 10

    def test_update_checkpoint_exception(self, session: Session, test_workspace: Workspace) -> None:
        from app.features.folder.change_log import update_checkpoint
        with patch.object(session, "commit", side_effect=Exception("DB fail")), \
             patch("app.features.folder.change_log.logger"):
            with pytest.raises(Exception, match="DB fail"):
                update_checkpoint(session, test_workspace.id, 5)
        # 数据库失败必须传回整理入口。

    def test_load_incremental_context_no_changes(self, session: Session, test_workspace: Workspace) -> None:
        from app.features.folder.change_log import load_incremental_context
        result = load_incremental_context(session, test_workspace.id)
        assert result["has_changes"] is False

    def test_load_incremental_context_with_events(self, session: Session, test_workspace: Workspace) -> None:
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

    def test_resolve_changed_details(self, session: Session, test_workspace: Workspace) -> None:
        from app.features.folder.change_log import _resolve_changed_details  # pyright: ignore[reportPrivateUsage]
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


# infra/cache.py 剩余分支
class TestCacheRemaining:
    def test_cacheable_prefix_attribute(self) -> None:
        from app.infra.cache import cacheable

        @cacheable(CacheableParams(prefix="myfunc", expire=60))
        def fn(x: int) -> int:
            return x

        assert getattr(fn, "cache_prefix", None) == "myfunc"

    def test_cacheable_async_prefix_attribute(self) -> None:
        from app.infra.cache import cacheable

        @cacheable(CacheableParams(prefix="asyncfunc", expire=60))
        async def fn(x: int) -> int:
            return x

        assert getattr(fn, "cache_prefix", None) == "asyncfunc"

    def test_cacheable_returns_none_default(self) -> None:
        """cache_none=False 且函数返回 None 时跳过缓存写入。"""
        from app.infra.cache import cacheable
        mock_redis = MagicMock()
        mock_redis.get.return_value = None

        @cacheable(CacheableParams(prefix="test", expire=60))
        def fn() -> None:
            return None

        with patch("app.infra.cache.redis_client", mock_redis):
            result = fn()
        assert result is None
        mock_redis.setex.assert_not_called()
