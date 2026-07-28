"""文件服务测试：上传/删除/搜索/分片/秒传等核心逻辑。"""

import json
import os
import tempfile
from io import BytesIO
from unittest.mock import patch, MagicMock

import pytest

from app.exceptions import (
    BusinessRuleError,
    PermissionDeniedError,
    ResourceNotFoundError,
    PayloadTooLargeError,
    ConflictError,
)
from app.models.file import File
from app.models.folder import Folder
from app.features.file.service import (
    _generate_unique_filename,
    _resolve_mime_type,
    _escape_like,
    _normalize_content_hash,
    _safe_upload_id,
    _list_uploaded_chunks,
    _env_int,
    get_file,
    get_authorized_file,
    get_files_and_folders,
    update_file,
    delete_file,
    preflight_file_upload,
    create_file,
    create_uploaded_file,
    batch_create_files,
    create_uploaded_files,
    init_multipart_upload,
    get_multipart_upload_status,
    save_multipart_chunk,
    complete_multipart_upload,
    abort_multipart_upload,
    search_files,
    get_all_files,
    process_status,
    batch_delete_items,
)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


class TestUtilFunctions:
    def test_generate_unique_filename(self):
        result = _generate_unique_filename("report.pdf")
        assert result.endswith(".pdf")
        assert "report" in result
        assert len(result) > len("report.pdf")

    def test_generate_unique_filename_unsafe(self):
        result = _generate_unique_filename("../../etc/passwd")
        assert ".." not in result

    def test_resolve_mime_type_provided(self):
        assert _resolve_mime_type("x.txt", "application/pdf") == "application/pdf"

    def test_resolve_mime_type_guess(self):
        result = _resolve_mime_type("photo.jpg")
        assert result == "image/jpeg"

    def test_resolve_mime_type_unknown(self):
        result = _resolve_mime_type("noext")
        assert result is None

    def test_escape_like(self):
        assert _escape_like("100%") == "100\\%"
        assert _escape_like("a_b") == "a\\_b"
        assert _escape_like("c\\d") == "c\\\\d"

    def test_normalize_content_hash_valid(self):
        h = "a" * 64
        assert _normalize_content_hash(h) == h

    def test_normalize_content_hash_uppercase(self):
        h = "A" * 64
        assert _normalize_content_hash(h) == h.lower()

    def test_normalize_content_hash_invalid(self):
        with pytest.raises(BusinessRuleError):
            _normalize_content_hash("short")

    def test_normalize_content_hash_none(self):
        assert _normalize_content_hash(None) is None
        assert _normalize_content_hash("") is None

    def test_safe_upload_id_valid(self):
        assert _safe_upload_id("abc12345") == "abc12345"

    def test_safe_upload_id_invalid(self):
        with pytest.raises(BusinessRuleError):
            _safe_upload_id("ab")  # 太短
        with pytest.raises(BusinessRuleError):
            _safe_upload_id("")

    def test_list_uploaded_chunks_empty(self, tmp_path):
        assert _list_uploaded_chunks(str(tmp_path / "nonexist")) == []

    def test_list_uploaded_chunks(self, tmp_path):
        chunks_dir = tmp_path / "chunks"
        chunks_dir.mkdir()
        (chunks_dir / "0.part").write_bytes(b"x")
        (chunks_dir / "2.part").write_bytes(b"x")
        (chunks_dir / "1.part").write_bytes(b"x")
        (chunks_dir / "meta.json").write_text("{}")  # 非 .part 忽略
        result = _list_uploaded_chunks(str(chunks_dir))
        assert result == [0, 1, 2]

    def test_env_int(self, monkeypatch):
        monkeypatch.setenv("TEST_INT_VAR", "42")
        assert _env_int("TEST_INT_VAR", 0) == 42
        assert _env_int("NONEXIST_VAR", 99) == 99


# ---------------------------------------------------------------------------
# 文件 CRUD
# ---------------------------------------------------------------------------


class TestGetFile:
    def test_get_existing(self, session, test_workspace, test_user):
        f = File(name="a.txt", file_path="a.txt", file_size=10,
                 workspace_id=test_workspace.id, uploader_id=test_user.id)
        session.add(f)
        session.flush()

        result = get_file(session, f.id)
        assert result.id == f.id

    def test_get_nonexistent(self, session):
        with pytest.raises(ResourceNotFoundError):
            get_file(session, 99999)


class TestGetAuthorizedFile:
    def test_authorized(self, session, test_workspace, test_user):
        f = File(name="b.txt", file_path="b.txt", file_size=10,
                 workspace_id=test_workspace.id, uploader_id=test_user.id)
        session.add(f)
        session.flush()

        result = get_authorized_file(session, test_workspace.id, test_user.id, f.id)
        assert result.id == f.id

    def test_wrong_workspace(self, session, test_workspace, test_user):
        f = File(name="c.txt", file_path="c.txt", file_size=10,
                 workspace_id=test_workspace.id, uploader_id=test_user.id)
        session.add(f)
        session.flush()

        with pytest.raises(PermissionDeniedError):
            get_authorized_file(session, 99999, test_user.id, f.id)


class TestUpdateFile:
    def test_rename(self, session, test_workspace, test_user):
        f = File(name="old.txt", file_path="old.txt", file_size=10,
                 workspace_id=test_workspace.id, uploader_id=test_user.id)
        session.add(f)
        session.flush()

        with patch("app.features.file.service.change_log_service") as mock_log, \
             patch("app.features.file.service._clear_search_cache"):
            updated = update_file(session, f.id, {"name": "new.txt"})
        assert updated.name == "new.txt"

    def test_move(self, session, test_workspace, test_user, test_folder):
        f = File(name="mv.txt", file_path="mv.txt", file_size=10,
                 workspace_id=test_workspace.id, uploader_id=test_user.id,
                 parent_id=None)
        session.add(f)
        session.flush()

        with patch("app.features.file.service.change_log_service") as mock_log, \
             patch("app.features.file.service._clear_search_cache"):
            updated = update_file(session, f.id, {"parent_id": test_folder.id})
        assert updated.parent_id == test_folder.id

    def test_update_nonexistent(self, session):
        with pytest.raises(ResourceNotFoundError):
            update_file(session, 99999, {"name": "x"})


class TestDeleteFile:
    def test_delete_existing(self, session, test_workspace, test_user):
        f = File(name="del.txt", file_path="del.txt", file_size=10,
                 workspace_id=test_workspace.id, uploader_id=test_user.id)
        session.add(f)
        session.flush()
        file_id = f.id

        with patch("app.features.file.service.change_log_service"), \
             patch("app.features.file.service._clear_search_cache"):
            delete_file(session, file_id)
        assert session.get(File, file_id) is None

    def test_delete_nonexistent(self, session):
        with pytest.raises(ResourceNotFoundError):
            delete_file(session, 99999)

    def test_delete_no_commit(self, session, test_workspace, test_user):
        f = File(name="nc.txt", file_path="nc.txt", file_size=10,
                 workspace_id=test_workspace.id, uploader_id=test_user.id)
        session.add(f)
        session.flush()
        file_id = f.id

        delete_file(session, file_id, commit=False, log_event=False)
        # 未 commit 但 session 中已标记删除


# ---------------------------------------------------------------------------
# 目录浏览
# ---------------------------------------------------------------------------


class TestGetFilesAndFolders:
    def test_basic_listing(self, session, test_workspace, test_user, test_folder):
        root = session.query(Folder).filter_by(
            workspace_id=test_workspace.id, parent_id=None
        ).first()

        # 添加文件到根目录
        f = File(name="file1.txt", file_path="f1.txt", file_size=100,
                 workspace_id=test_workspace.id, uploader_id=test_user.id,
                 parent_id=root.id)
        session.add(f)
        session.flush()

        result = get_files_and_folders(session, test_workspace.id, root.id)
        assert "folders" in result
        assert "files" in result
        assert result["files"]["total"] >= 1

    def test_name_filter(self, session, test_workspace, test_user):
        root = session.query(Folder).filter_by(
            workspace_id=test_workspace.id, parent_id=None
        ).first()
        f = File(name="report_2024.pdf", file_path="r.pdf", file_size=100,
                 workspace_id=test_workspace.id, uploader_id=test_user.id,
                 parent_id=root.id)
        session.add(f)
        session.flush()

        result = get_files_and_folders(session, test_workspace.id, root.id, name="report")
        assert result["files"]["total"] >= 1

    def test_sort_by_name(self, session, test_workspace, test_user):
        root = session.query(Folder).filter_by(
            workspace_id=test_workspace.id, parent_id=None
        ).first()
        result = get_files_and_folders(
            session, test_workspace.id, root.id, sort_by="name", order="asc"
        )
        assert result is not None

    def test_pagination(self, session, test_workspace, test_user):
        root = session.query(Folder).filter_by(
            workspace_id=test_workspace.id, parent_id=None
        ).first()
        for i in range(5):
            f = File(name=f"pg{i}.txt", file_path=f"pg{i}.txt", file_size=10,
                     workspace_id=test_workspace.id, uploader_id=test_user.id,
                     parent_id=root.id)
            session.add(f)
        session.flush()

        result = get_files_and_folders(session, test_workspace.id, root.id, page=1, page_size=2)
        assert result["files"]["pages"] >= 3


# ---------------------------------------------------------------------------
# 搜索
# ---------------------------------------------------------------------------


class TestSearchFiles:
    async def test_fuzzy_search(self, session, test_workspace, test_user):
        f = File(name="searchable.txt", file_path="s.txt", file_size=10,
                 workspace_id=test_workspace.id, uploader_id=test_user.id, status="success")
        session.add(f)
        session.flush()

        result = await search_files(session, test_workspace.id, "searchable")
        assert result["total"] >= 1

    async def test_empty_query(self, session, test_workspace, test_user):
        result = await search_files(session, test_workspace.id, "")
        assert result["total"] == 0


# ---------------------------------------------------------------------------
# 上传预检 / 秒传
# ---------------------------------------------------------------------------


class TestPreflightUpload:
    def test_missing_filename(self, session, test_workspace, test_user):
        with pytest.raises(BusinessRuleError):
            preflight_file_upload(session, test_workspace.id, test_user.id, {
                "filename": "", "total_size": 100, "content_hash": "a" * 64
            })

    def test_missing_size(self, session, test_workspace, test_user):
        with pytest.raises(BusinessRuleError):
            preflight_file_upload(session, test_workspace.id, test_user.id, {
                "filename": "f.txt", "total_size": 0, "content_hash": "a" * 64
            })

    def test_no_match(self, session, test_workspace, test_user):
        result = preflight_file_upload(session, test_workspace.id, test_user.id, {
            "filename": "new.txt", "total_size": 100, "content_hash": "b" * 64
        })
        assert result["instant_upload"] is False


# ---------------------------------------------------------------------------
# 分片上传
# ---------------------------------------------------------------------------


class TestMultipartUpload:
    def test_init_missing_filename(self, session, test_workspace, test_user):
        with pytest.raises(BusinessRuleError):
            init_multipart_upload(session, test_workspace.id, test_user.id, {
                "filename": "", "total_size": 100
            })

    def test_init_invalid_size(self, session, test_workspace, test_user):
        with pytest.raises(BusinessRuleError):
            init_multipart_upload(session, test_workspace.id, test_user.id, {
                "filename": "f.txt", "total_size": 0
            })

    def test_init_invalid_chunk_size(self, session, test_workspace, test_user):
        with pytest.raises(BusinessRuleError):
            init_multipart_upload(session, test_workspace.id, test_user.id, {
                "filename": "f.txt", "total_size": 100, "chunk_size": -1
            })

    def test_init_and_status(self, session, test_workspace, test_user, tmp_path):
        with patch("app.features.file.service.MULTIPART_ROOT", str(tmp_path)):
            result = init_multipart_upload(session, test_workspace.id, test_user.id, {
                "filename": "big.bin", "total_size": 1024, "chunk_size": 512
            })
            assert result["upload_id"] is not None
            assert result["total_chunks"] == 2
            assert result["instant_upload"] is False

            # 查询状态
            status = get_multipart_upload_status(test_workspace.id, result["upload_id"])
            assert status["total_chunks"] == 2
            assert status["uploaded_chunks"] == []

    def test_abort(self, session, test_workspace, test_user, tmp_path):
        with patch("app.features.file.service.MULTIPART_ROOT", str(tmp_path)):
            result = init_multipart_upload(session, test_workspace.id, test_user.id, {
                "filename": "abort.bin", "total_size": 100, "chunk_size": 100
            })
            abort_multipart_upload(test_workspace.id, result["upload_id"])
            # 中止后查询应报错
            with pytest.raises(ResourceNotFoundError):
                get_multipart_upload_status(test_workspace.id, result["upload_id"])


# ---------------------------------------------------------------------------
# 文件上传（mock 文件系统）
# ---------------------------------------------------------------------------


class TestCreateFile:
    def test_create_file(self, session, test_workspace, test_user, tmp_path):
        mock_upload = MagicMock()
        mock_upload.filename = "test.txt"
        mock_upload.mimetype = "text/plain"
        mock_upload.save = lambda path: open(path, "wb").write(b"hello")

        with patch("app.features.file.service.UPLOAD_FOLDER", str(tmp_path)), \
             patch("app.features.file.service.change_log_service"), \
             patch("app.features.file.service._push_processing_queue"):
            f = create_file(session, mock_upload, {
                "workspace_id": test_workspace.id,
                "uploader_id": test_user.id,
                "parent_id": None,
            })
        assert f.id is not None
        assert f.name == "test.txt"
        assert f.file_size == 5

    def test_create_uploaded_file_no_filename(self, session, test_workspace, test_user):
        mock_upload = MagicMock()
        mock_upload.filename = None
        with pytest.raises(BusinessRuleError):
            create_uploaded_file(session, test_workspace.id, test_user.id, mock_upload)


class TestBatchCreateFiles:
    def test_batch_empty(self, session, test_workspace, test_user):
        result = batch_create_files(session, [], {"workspace_id": test_workspace.id})
        assert result == []

    def test_create_uploaded_files_no_valid(self, session, test_workspace, test_user):
        mock_upload = MagicMock()
        mock_upload.filename = None
        with pytest.raises(BusinessRuleError):
            create_uploaded_files(session, test_workspace.id, test_user.id, [mock_upload])


# ---------------------------------------------------------------------------
# 批量删除 / 状态
# ---------------------------------------------------------------------------


class TestBatchDeleteAndStatus:
    def test_get_all_files(self, session, test_workspace, test_user):
        f = File(name="all.txt", file_path="all.txt", file_size=10,
                 workspace_id=test_workspace.id, uploader_id=test_user.id)
        session.add(f)
        session.flush()

        files = get_all_files(session, test_workspace.id)
        assert len(files) >= 1

    async def test_process_status(self, session, test_workspace, test_user):
        session.add(File(name="s1.txt", file_path="s1.txt", file_size=10,
                         workspace_id=test_workspace.id, status="success"))
        session.add(File(name="s2.txt", file_path="s2.txt", file_size=10,
                         workspace_id=test_workspace.id, status="pending"))
        session.flush()

        status = await process_status(session, test_workspace.id)
        assert "成功" in status
        assert status["成功"] >= 1
