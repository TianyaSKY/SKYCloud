# pyright: reportPrivateUsage=false
"""Bloom 过滤器 + 变更日志 + 摘要测试。"""

from typing import Any, cast
from unittest.mock import MagicMock, patch

from sqlalchemy.orm import Session

from app.features.file.bloom import (
    BLOOM_MIN_BITS,
    _bitmap_size,
    _bits_key,
    _build_filter,
    _ensure_filter_ready,
    _hash_positions,
    _lock_key,
    _maybe_contains,
    _meta_key,
    _scope_name,
    add_file,
    maybe_file_exists,
    maybe_user_can_access_file,
)
from app.features.folder.change_log import (
    LogEventParams,
    _build_folder_path,
    _to_payload_text,
    get_checkpoint_event_id,
    get_latest_event_id,
    load_incremental_context,
    log_event,
    log_events_batch,
    update_checkpoint,
)
from app.features.folder.change_log_summary import _event_value, summarize_events
from app.models.file_change_event import FileChangeEvent
from app.models.organize_checkpoint import OrganizeCheckpoint
from app.models.workspace import Workspace


# Bloom 工具函数


class TestBloomUtils:
    def test_scope_name_none(self) -> None:
        assert _scope_name(None) == "all"

    def test_scope_name_user(self) -> None:
        assert _scope_name(42) == "user:42"

    def test_bits_key(self) -> None:
        assert _bits_key(None) == "file_access:bloom:all:bits"
        assert _bits_key(1) == "file_access:bloom:user:1:bits"

    def test_meta_key(self) -> None:
        assert _meta_key(None) == "file_access:bloom:all:meta"

    def test_lock_key(self) -> None:
        assert _lock_key(None) == "file_access:bloom:all:lock"

    def test_bitmap_size_minimum(self) -> None:
        assert _bitmap_size(0) == BLOOM_MIN_BITS
        assert _bitmap_size(1) == BLOOM_MIN_BITS

    def test_bitmap_size_large(self) -> None:
        result = _bitmap_size(10000)
        assert result >= 10000

    def test_hash_positions_deterministic(self) -> None:
        pos1 = _hash_positions(123, 4096, 7)
        pos2 = _hash_positions(123, 4096, 7)
        assert pos1 == pos2
        assert len(pos1) == 7
        assert all(0 <= p < 4096 for p in pos1)

    def test_hash_positions_different_ids(self) -> None:
        pos1 = _hash_positions(1, 4096, 7)
        pos2 = _hash_positions(2, 4096, 7)
        assert pos1 != pos2


# Bloom 构建与查询（mock redis）


class TestBloomBuild:
    def test_build_filter_lock_fail(self) -> None:
        mock_redis = MagicMock()
        mock_redis.set.return_value = False  # 锁获取失败
        with patch("app.features.file.bloom.redis_client", mock_redis):
            assert _build_filter(None) is False

    def test_build_filter_success(self) -> None:
        mock_redis = MagicMock()
        mock_redis.set.return_value = True  # 锁获取成功
        mock_redis.get.return_value = "token"  # 释放锁时匹配
        mock_pipe = MagicMock()
        mock_redis.pipeline.return_value = mock_pipe

        with patch("app.features.file.bloom.redis_client", mock_redis), \
             patch("app.features.file.bloom._iter_file_ids", return_value=[1, 2, 3]):
            result = _build_filter(None)
        assert result is True
        assert mock_pipe.execute.call_count == 2

    def test_build_filter_exception(self) -> None:
        mock_redis = MagicMock()
        mock_redis.set.return_value = True
        mock_redis.get.return_value = "token"
        mock_redis.pipeline.side_effect = Exception("redis down")

        with patch("app.features.file.bloom.redis_client", mock_redis), \
             patch("app.features.file.bloom._iter_file_ids", return_value=[1]):
            result = _build_filter(None)
        assert result is False

    def test_build_lock_release_failure_preserves_completed_filter(self) -> None:
        redis = MagicMock()
        redis.set.return_value = True
        redis.get.side_effect = RuntimeError("lock lookup unavailable")
        with patch("app.features.file.bloom.redis_client", redis), \
             patch("app.features.file.bloom._iter_file_ids", return_value=[1]):
            assert _build_filter(None) is True
        assert redis.pipeline.return_value.execute.call_count == 2


class TestEnsureFilterReady:
    def test_already_ready(self) -> None:
        mock_redis = MagicMock()
        mock_redis.hget.return_value = "1"
        with patch("app.features.file.bloom.redis_client", mock_redis):
            assert _ensure_filter_ready(None) is True

    def test_not_ready_build_success(self) -> None:
        mock_redis = MagicMock()
        mock_redis.hget.return_value = None
        with patch("app.features.file.bloom.redis_client", mock_redis), \
             patch("app.features.file.bloom._build_filter", return_value=True):
            assert _ensure_filter_ready(None) is True

    def test_exception(self) -> None:
        mock_redis = MagicMock()
        mock_redis.hget.side_effect = Exception("err")
        with patch("app.features.file.bloom.redis_client", mock_redis):
            assert _ensure_filter_ready(None) is False


class TestMaybeContains:
    def test_filter_not_ready(self) -> None:
        with patch("app.features.file.bloom._ensure_filter_ready", return_value=False):
            assert _maybe_contains(None, 1) is True  # 降级为 True

    def test_maybe_contains_true(self) -> None:
        mock_redis = MagicMock()
        mock_redis.hgetall.return_value = {"bitmap_size": "4096", "hash_count": "7"}
        mock_pipe = MagicMock()
        mock_pipe.execute.return_value = [1, 1, 1, 1, 1, 1, 1]
        mock_redis.pipeline.return_value = mock_pipe

        with patch("app.features.file.bloom.redis_client", mock_redis), \
             patch("app.features.file.bloom._ensure_filter_ready", return_value=True):
            assert _maybe_contains(None, 42) is True

    def test_maybe_contains_false(self) -> None:
        mock_redis = MagicMock()
        mock_redis.hgetall.return_value = {"bitmap_size": "4096", "hash_count": "7"}
        mock_pipe = MagicMock()
        mock_pipe.execute.return_value = [1, 0, 1, 1, 1, 1, 1]
        mock_redis.pipeline.return_value = mock_pipe

        with patch("app.features.file.bloom.redis_client", mock_redis), \
             patch("app.features.file.bloom._ensure_filter_ready", return_value=True):
            assert _maybe_contains(None, 42) is False

    def test_invalid_meta(self) -> None:
        mock_redis = MagicMock()
        mock_redis.hgetall.return_value = {"bitmap_size": "0", "hash_count": "0"}

        with patch("app.features.file.bloom.redis_client", mock_redis), \
             patch("app.features.file.bloom._ensure_filter_ready", return_value=True):
            assert _maybe_contains(None, 42) is True

    def test_lookup_failure_defers_to_database_authorization(self) -> None:
        redis = MagicMock()
        redis.hgetall.side_effect = RuntimeError("bitmap unavailable")
        with patch("app.features.file.bloom.redis_client", redis), \
             patch("app.features.file.bloom._ensure_filter_ready", return_value=True):
            assert _maybe_contains(7, 42) is True


class TestAddFile:
    def test_add_file_not_ready(self) -> None:
        mock_redis = MagicMock()
        mock_redis.hgetall.return_value = {"ready": "0"}
        with patch("app.features.file.bloom.redis_client", mock_redis):
            add_file(1, 1)  # 不应报错

    def test_add_file_ready(self) -> None:
        mock_redis = MagicMock()
        mock_redis.hgetall.return_value = {"ready": "1", "bitmap_size": "4096", "hash_count": "7"}
        mock_pipe = MagicMock()
        mock_redis.pipeline.return_value = mock_pipe

        with patch("app.features.file.bloom.redis_client", mock_redis):
            add_file(1, 1)
        assert mock_pipe.execute.called

    def test_failed_scope_update_does_not_prevent_remaining_scope(self) -> None:
        redis = MagicMock()
        redis.hgetall.return_value = {
            "ready": "1", "bitmap_size": "4096", "hash_count": "7",
        }
        redis.pipeline.side_effect = RuntimeError("bitmap update unavailable")
        with patch("app.features.file.bloom.redis_client", redis):
            add_file(42, 7)
        assert [
            args.args[0] for args in redis.hgetall.call_args_list
        ] == [_meta_key(None), _meta_key(7)]


class TestPublicAPI:
    def test_maybe_user_can_access(self) -> None:
        with patch("app.features.file.bloom._maybe_contains", return_value=True) as m:
            assert maybe_user_can_access_file(1, 10) is True
            m.assert_called_once_with(1, 10)

    def test_maybe_file_exists(self) -> None:
        with patch("app.features.file.bloom._maybe_contains", return_value=False) as m:
            assert maybe_file_exists(10) is False
            m.assert_called_once_with(None, 10)


# change_log_summary


class TestEventValue:
    def test_dict(self) -> None:
        assert _event_value({"key": "val"}, "key") == "val"
        assert _event_value({}, "key", "def") == "def"

    def test_object(self) -> None:
        class Obj:
            name = "test"

        assert _event_value(cast(Any, Obj()), "name") == "test"
        assert _event_value(cast(Any, Obj()), "missing", "def") == "def"


class TestSummarizeEvents:
    def test_empty(self) -> None:
        result = summarize_events([], total_count=0, from_event_id=0, to_event_id=0)
        assert result["changed_file_ids"] == []
        assert result["changed_folder_ids"] == []
        assert result["action_breakdown"] == {}

    def test_basic_events(self) -> None:
        events: list[dict[str, int | float | str | None]] = [
            {"id": 1, "entity_type": "file", "entity_id": 10, "action": "create",
             "old_parent_id": None, "new_parent_id": 5, "old_name": None, "new_name": "a.txt"},
            {"id": 2, "entity_type": "folder", "entity_id": 5, "action": "rename",
             "old_parent_id": None, "new_parent_id": None, "old_name": "old", "new_name": "new"},
        ]
        result = summarize_events(events, total_count=2, from_event_id=0, to_event_id=2)
        assert 10 in result["changed_file_ids"]
        assert 5 in result["changed_folder_ids"]
        assert "file:create" in result["action_breakdown"]
        assert "Event range: (0, 2]" in result["summary_text"]

    def test_max_lines(self) -> None:
        events: list[dict[str, int | float | str | None]] = [
            {"id": i, "entity_type": "file", "entity_id": i, "action": "create",
             "old_parent_id": None, "new_parent_id": None, "old_name": None, "new_name": f"f{i}"}
            for i in range(1, 100)
        ]
        result = summarize_events(events, total_count=99, from_event_id=0, to_event_id=99, max_lines=5)
        # summary_text 中 Sample changes 行不超过 max_lines
        assert result["summary_text"] is not None


# change_log 核心函数


class TestBuildFolderPath:
    def test_none(self) -> None:
        assert _build_folder_path(None, {}) == "根目录"
        assert _build_folder_path(0, {}) == "根目录"

    def test_simple_path(self) -> None:
        class FakeFolder:
            def __init__(self, id: int, name: str, parent_id: int | None) -> None:
                self.id = id
                self.name = name
                self.parent_id = parent_id

        folder_map: dict[int, Any] = {
            1: FakeFolder(1, "docs", None),
            2: FakeFolder(2, "sub", 1),
        }
        assert _build_folder_path(2, folder_map) == "根目录/docs/sub"

    def test_cycle_detection(self) -> None:
        class FakeFolder:
            def __init__(self, id: int, name: str, parent_id: int | None) -> None:
                self.id = id
                self.name = name
                self.parent_id = parent_id

        folder_map: dict[int, Any] = {
            1: FakeFolder(1, "a", 2),
            2: FakeFolder(2, "b", 1),
        }
        # 不死循环即可
        result = _build_folder_path(1, folder_map)
        assert "根目录" in result


class TestToPayloadText:
    def test_none(self) -> None:
        assert _to_payload_text(None) is None

    def test_string(self) -> None:
        assert _to_payload_text("hello") == "hello"

    def test_dict(self) -> None:
        result = _to_payload_text({"key": "val"})
        assert result is not None and "key" in result


class TestLogEventsBatch:
    def test_empty(self) -> None:
        assert log_events_batch(1, []) == 0

    def test_no_valid_entity(self) -> None:
        assert log_events_batch(1, [cast(Any, {"entity_id": None})]) == 0

    def test_success(self) -> None:
        mock_session = MagicMock()
        with patch("app.features.folder.change_log.SessionLocal", return_value=mock_session):
            count = log_events_batch(1, [
                {"entity_type": "file", "entity_id": 1, "action": "create"},
                {"entity_type": "file", "entity_id": 2, "action": "delete"},
            ])
        assert count == 2
        assert mock_session.commit.called

    def test_db_error(self) -> None:
        mock_session = MagicMock()
        mock_session.commit.side_effect = Exception("db error")
        with patch("app.features.folder.change_log.SessionLocal", return_value=mock_session):
            count = log_events_batch(1, [{"entity_type": "file", "entity_id": 1, "action": "create"}])
        assert count == 0


class TestLogEvent:
    def test_success(self) -> None:
        with patch("app.features.folder.change_log.log_events_batch", return_value=1):
            assert log_event(LogEventParams(
                workspace_id=1, entity_type="file", entity_id=1, action="create",
            )) is True

    def test_failure(self) -> None:
        with patch("app.features.folder.change_log.log_events_batch", return_value=0):
            assert log_event(LogEventParams(
                workspace_id=1, entity_type="file", entity_id=1, action="create",
            )) is False


class TestGetLatestEventId:
    def test_no_events(self, session: Session, test_workspace: Workspace) -> None:
        assert get_latest_event_id(session, test_workspace.id) == 0

    def test_with_events(self, session: Session, test_workspace: Workspace) -> None:
        session.add(FileChangeEvent(workspace_id=test_workspace.id, entity_type="file", entity_id=1, action="create"))
        session.flush()
        assert get_latest_event_id(session, test_workspace.id) >= 1


class TestCheckpoint:
    def test_get_checkpoint_no_record(self, session: Session, test_workspace: Workspace) -> None:
        assert get_checkpoint_event_id(session, test_workspace.id) == 0

    def test_update_checkpoint_create(self, session: Session, test_workspace: Workspace) -> None:
        update_checkpoint(session, test_workspace.id, 10)
        assert get_checkpoint_event_id(session, test_workspace.id) == 10

    def test_update_checkpoint_increase_only(self, session: Session, test_workspace: Workspace) -> None:
        update_checkpoint(session, test_workspace.id, 10)
        update_checkpoint(session, test_workspace.id, 5)  # 不应回退
        assert get_checkpoint_event_id(session, test_workspace.id) == 10

    def test_update_checkpoint_mark_full_scan(self, session: Session, test_workspace: Workspace) -> None:
        update_checkpoint(session, test_workspace.id, 1, mark_full_scan=True)
        cp = session.query(OrganizeCheckpoint).filter_by(workspace_id=test_workspace.id).first()
        assert cp is not None and cp.last_full_scan_at is not None


class TestLoadIncrementalContext:
    def test_no_changes(self, session: Session, test_workspace: Workspace) -> None:
        result = load_incremental_context(session, test_workspace.id)
        assert result["has_changes"] is False
        assert result["total_events"] == 0

    def test_with_changes(self, session: Session, test_workspace: Workspace) -> None:
        session.add(FileChangeEvent(
            workspace_id=test_workspace.id, entity_type="file", entity_id=1,
            action="create", new_parent_id=None, new_name="test.txt"
        ))
        session.flush()
        result = load_incremental_context(session, test_workspace.id)
        assert result["has_changes"] is True
        assert result["total_events"] is not None and result["total_events"] >= 1
