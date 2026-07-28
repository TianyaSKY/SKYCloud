"""文件服务扩展测试：embedding/重建索引/清理/头像/批量删除等。"""

import os
import time
import pytest
from unittest.mock import patch, MagicMock

from app.exceptions import PermissionDeniedError, ResourceNotFoundError
from app.models.file import File
from app.models.folder import Folder
from app.features.file.service import (
    embedding_desc,
    batch_embedding_desc,
    retry_embedding,
    rebuild_failed_indexes,
    cleanup_expired_uploads,
    get_root_file_id,
    batch_delete_items,
    upload_avatar_for_user,
    _calculate_file_hash,
    _get_reusable_source_file,
    _persist_file_record,
    _push_processing_queue,
    _clear_search_cache,
    _multipart_upload_dir,
    _multipart_chunks_dir,
    _multipart_meta_path,
    _load_multipart_meta,
    _write_multipart_meta,
)


# ---------------------------------------------------------------------------
# Embedding 相关
# ---------------------------------------------------------------------------


class TestEmbeddingDesc:
    def test_exception_returns_empty(self):
        with patch("app.infra.llm.client.embed_texts", side_effect=Exception("api err")):
            result = embedding_desc("hello", {"api": "a", "key": "k", "model": "m"})
        assert result == []


class TestBatchEmbeddingDesc:
    def test_empty_texts(self):
        result = batch_embedding_desc([], {"api": "a", "key": "k", "model": "m"})
        assert result == []

    def test_exception_returns_placeholders(self):
        with patch("app.infra.llm.client.embed_texts", side_effect=Exception("err")):
            result = batch_embedding_desc(["a", "b"], {"api": "a", "key": "k", "model": "m"})
        assert result == [[], []]


# ---------------------------------------------------------------------------
# 索引重试与重建
# ---------------------------------------------------------------------------


class TestRetryEmbedding:
    def test_file_not_found(self, session):
        retry_embedding(session, 99999)  # 不应报错

    def test_file_found(self, session, test_workspace, test_user):
        f = File(name="r.txt", file_path="r.txt", file_size=10,
                 workspace_id=test_workspace.id, uploader_id=test_user.id, status="fail")
        session.add(f)
        session.flush()

        with patch("app.features.file.service.publish_file_tasks") as m:
            retry_embedding(session, f.id)
        m.assert_called_once()


class TestRebuildFailedIndexes:
    def test_no_failed_files(self, session, test_workspace):
        count = rebuild_failed_indexes(session, test_workspace.id)
        assert count == 0

    def test_with_failed_files(self, session, test_workspace, test_user):
        session.add(File(name="f1.txt", file_path="f1.txt", file_size=10,
                         workspace_id=test_workspace.id, status="fail"))
        session.add(File(name="f2.txt", file_path="f2.txt", file_size=10,
                         workspace_id=test_workspace.id, status="fail"))
        session.flush()

        with patch("app.features.file.service.publish_file_tasks"):
            count = rebuild_failed_indexes(session, test_workspace.id)
        assert count == 2

    def test_publish_error(self, session, test_workspace, test_user):
        session.add(File(name="f3.txt", file_path="f3.txt", file_size=10,
                         workspace_id=test_workspace.id, status="fail"))
        session.flush()

        with patch("app.features.file.service.publish_file_tasks", side_effect=Exception("mq err")):
            count = rebuild_failed_indexes(session, test_workspace.id)
        assert count == 0


# ---------------------------------------------------------------------------
# 清理过期上传
# ---------------------------------------------------------------------------


class TestCleanupExpiredUploads:
    def test_no_multipart_root(self, tmp_path):
        with patch("app.features.file.service.MULTIPART_ROOT", str(tmp_path / "nonexist")):
            assert cleanup_expired_uploads() == 0

    def test_cleanup_old_dirs(self, tmp_path):
        # 创建过期的上传目录
        ws_dir = tmp_path / "1"
        upload_dir = ws_dir / "upload123"
        upload_dir.mkdir(parents=True)
        # 设置很旧的修改时间
        old_time = time.time() - 100000
        os.utime(str(upload_dir), (old_time, old_time))

        with patch("app.features.file.service.MULTIPART_ROOT", str(tmp_path)):
            count = cleanup_expired_uploads(max_age_hours=1)
        assert count == 1
        assert not upload_dir.exists()

    def test_keep_recent_dirs(self, tmp_path):
        ws_dir = tmp_path / "1"
        upload_dir = ws_dir / "upload456"
        upload_dir.mkdir(parents=True)

        with patch("app.features.file.service.MULTIPART_ROOT", str(tmp_path)):
            count = cleanup_expired_uploads(max_age_hours=24)
        assert count == 0
        assert upload_dir.exists()


# ---------------------------------------------------------------------------
# 根文件 ID 缓存
# ---------------------------------------------------------------------------


class TestGetRootFileId:
    def test_cached(self, session, test_workspace):
        mock_redis = MagicMock()
        mock_redis.get.return_value = "42"
        with patch("app.features.file.service.redis_client", mock_redis):
            result = get_root_file_id(session, test_workspace.id)
        assert result == 42

    def test_from_db(self, session, test_workspace, test_user):
        mock_redis = MagicMock()
        mock_redis.get.return_value = None
        f = File(name="root.txt", file_path="root.txt", file_size=10,
                 workspace_id=test_workspace.id, parent_id=None)
        session.add(f)
        session.flush()

        with patch("app.features.file.service.redis_client", mock_redis):
            result = get_root_file_id(session, test_workspace.id)
        assert result == f.id
        mock_redis.set.assert_called()

    def test_not_found(self, session, test_workspace):
        mock_redis = MagicMock()
        mock_redis.get.return_value = None
        # 确保没有 parent_id=None 的文件
        session.query(File).filter_by(workspace_id=test_workspace.id, parent_id=None).delete()
        session.flush()
        with patch("app.features.file.service.redis_client", mock_redis):
            result = get_root_file_id(session, test_workspace.id)
        assert result is None


# ---------------------------------------------------------------------------
# 批量删除
# ---------------------------------------------------------------------------


class TestBatchDeleteItems:
    def test_delete_file(self, session, test_workspace, test_user):
        f = File(name="bd.txt", file_path="bd.txt", file_size=10,
                 workspace_id=test_workspace.id, uploader_id=test_user.id)
        session.add(f)
        session.flush()
        file_id = f.id

        with patch("app.features.file.service.change_log_service"), \
             patch("app.features.file.service._clear_search_cache"):
            batch_delete_items(session, test_workspace.id, test_user.id, [{"id": file_id, "is_folder": False}])
        assert session.get(File, file_id) is None

    def test_delete_folder(self, session, test_workspace, test_user, test_folder):
        from app.features.folder.service import get_folder
        folder_id = test_folder.id

        with patch("app.features.file.service.change_log_service"), \
             patch("app.features.file.service._clear_search_cache"):
            batch_delete_items(session, test_workspace.id, test_user.id, [{"id": folder_id, "is_folder": True}])
        assert session.get(Folder, folder_id) is None


# ---------------------------------------------------------------------------
# 头像上传权限
# ---------------------------------------------------------------------------


class TestUploadAvatarForUser:
    def test_permission_denied(self, session):
        with pytest.raises(PermissionDeniedError):
            upload_avatar_for_user(session, actor_id=1, actor_role="user", user_id=2, upload=MagicMock())

    def test_admin_allowed(self, session):
        with patch("app.features.file.service.upload_avatar", return_value={"avatar_url": "/x"}) as m:
            result = upload_avatar_for_user(session, actor_id=1, actor_role="admin", user_id=2, upload=MagicMock())
        assert result["avatar_url"] == "/x"

    def test_self_allowed(self, session):
        with patch("app.features.file.service.upload_avatar", return_value={"avatar_url": "/y"}) as m:
            result = upload_avatar_for_user(session, actor_id=5, actor_role="user", user_id=5, upload=MagicMock())
        assert result["avatar_url"] == "/y"


# ---------------------------------------------------------------------------
# 内部工具函数
# ---------------------------------------------------------------------------


class TestInternalUtils:
    def test_calculate_file_hash(self, tmp_path):
        f = tmp_path / "test.bin"
        f.write_bytes(b"hello world")
        h = _calculate_file_hash(str(f))
        assert len(h) == 64  # SHA-256 hex

    def test_get_reusable_source_file_not_found(self, session, test_workspace):
        result = _get_reusable_source_file(session, "a" * 64, 100)
        assert result is None

    def test_push_processing_queue_empty(self):
        _push_processing_queue([], 1)  # 不应报错

    def test_push_processing_queue_error(self):
        with patch("app.features.file.service.publish_file_tasks", side_effect=Exception("err")):
            _push_processing_queue([1], 1)  # 不应抛异常

    def test_clear_search_cache(self):
        with patch("app.features.file.service.evict_cache_pattern") as m:
            _clear_search_cache(1)
            m.assert_called_once()

    def test_multipart_paths(self):
        with patch("app.features.file.service.MULTIPART_ROOT", "/base"):
            d = _multipart_upload_dir(1, "abc12345")
            assert "1" in d
            assert "abc12345" in d
            assert _multipart_chunks_dir(d).endswith("chunks")
            assert _multipart_meta_path(d).endswith("meta.json")

    def test_write_and_load_meta(self, tmp_path, test_workspace):
        import json
        meta_path = str(tmp_path / "meta.json")
        meta = {"workspace_id": test_workspace.id, "filename": "test.bin", "total_size": 100}
        _write_multipart_meta(meta_path, meta)

        with patch("app.features.file.service.MULTIPART_ROOT", str(tmp_path.parent)):
            # 创建正确的目录结构
            upload_dir = tmp_path.parent / str(test_workspace.id) / "abc12345"
            upload_dir.mkdir(parents=True, exist_ok=True)
            meta_file = upload_dir / "meta.json"
            meta_file.write_text(json.dumps(meta))

            loaded = _load_multipart_meta(test_workspace.id, "abc12345")
            assert loaded["filename"] == "test.bin"

    def test_load_meta_wrong_workspace(self, tmp_path, test_workspace):
        import json
        upload_dir = tmp_path / str(test_workspace.id) / "abc12345"
        upload_dir.mkdir(parents=True, exist_ok=True)
        meta_file = upload_dir / "meta.json"
        meta_file.write_text(json.dumps({"workspace_id": 99999}))

        with patch("app.features.file.service.MULTIPART_ROOT", str(tmp_path)):
            with pytest.raises(PermissionDeniedError):
                _load_multipart_meta(test_workspace.id, "abc12345")
