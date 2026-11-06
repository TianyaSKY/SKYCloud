"""文件服务测试：上传/删除/搜索/分片/秒传等核心逻辑。"""

from io import BytesIO
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from fastapi import UploadFile
import pytest
from sqlalchemy.orm import Session
from starlette.datastructures import Headers

from app.exceptions import (
    BusinessRuleError,
    ConflictError,
    PermissionDeniedError,
    ResourceNotFoundError,
)
from app.features.file.multipart_service import MultipartInitData, get_multipart_service
from app.features.file.repository import escape_like
from app.features.file.search_service import SearchFilesParams, search_files
from app.features.file.service import (
    GetFilesAndFoldersParams,
    delete_file,
    get_all_files,
    get_authorized_file,
    get_file,
    get_files_and_folders,
    process_status,
    update_file,
)
from app.features.file.upload_service import get_upload_service
from app.features.file.upload_types import PreflightData, UploadSource
from app.infra.upload_adapter import FastAPIUploadAdapter
from app.infra.upload_limits import positive_limit
from app.models import File, Folder, User, Workspace


# 工具函数


def _upload(filename: str | None, payload: bytes, mimetype: str | None) -> FastAPIUploadAdapter:
    headers = Headers({"content-type": mimetype}) if mimetype else None
    return FastAPIUploadAdapter(UploadFile(
        filename=filename, file=BytesIO(payload), headers=headers,
    ))


class TestUtilFunctions:
    def test_escape_like(self) -> None:
        assert escape_like("100%") == "100\\%"
        assert escape_like("a_b") == "a\\_b"
        assert escape_like("c\\d") == "c\\\\d"

    def test_positive_upload_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_INT_VAR", "42")
        assert positive_limit("TEST_INT_VAR", 0) == 42
        assert positive_limit("NONEXIST_VAR", 99) == 99


# 文件 CRUD


class TestGetFile:
    def test_get_existing(self, session: Session, test_workspace: Workspace, test_user: User) -> None:
        f = File(name="a.txt", file_path="a.txt", file_size=10,
                 workspace_id=test_workspace.id, uploader_id=test_user.id)
        session.add(f)
        session.flush()

        result = get_file(session, f.id)
        assert result.id == f.id

    def test_get_nonexistent(self, session: Session) -> None:
        with pytest.raises(ResourceNotFoundError):
            get_file(session, 99999)


class TestGetAuthorizedFile:
    def test_authorized(self, session: Session, test_workspace: Workspace, test_user: User) -> None:
        f = File(name="b.txt", file_path="b.txt", file_size=10,
                 workspace_id=test_workspace.id, uploader_id=test_user.id)
        session.add(f)
        session.flush()

        result = get_authorized_file(session, test_workspace.id, test_user.id, f.id)
        assert result.id == f.id

    def test_wrong_workspace(self, session: Session, test_workspace: Workspace, test_user: User) -> None:
        f = File(name="c.txt", file_path="c.txt", file_size=10,
                 workspace_id=test_workspace.id, uploader_id=test_user.id)
        session.add(f)
        session.flush()

        with pytest.raises(PermissionDeniedError):
            get_authorized_file(session, 99999, test_user.id, f.id)


class TestUpdateFile:
    def test_rename(self, session: Session, test_workspace: Workspace, test_user: User) -> None:
        f = File(name="old.txt", file_path="old.txt", file_size=10,
                 workspace_id=test_workspace.id, uploader_id=test_user.id)
        session.add(f)
        session.flush()

        with patch("app.features.file.service.change_log_service"), \
             patch("app.features.file.service.clear_search_cache"):
            updated = update_file(session, f.id, {"name": "new.txt"})
        assert updated.name == "new.txt"

    def test_move(self, session: Session, test_workspace: Workspace, test_user: User, test_folder: Folder) -> None:
        f = File(name="mv.txt", file_path="mv.txt", file_size=10,
                 workspace_id=test_workspace.id, uploader_id=test_user.id,
                 parent_id=None)
        session.add(f)
        session.flush()

        with patch("app.features.file.service.change_log_service"), \
             patch("app.features.file.service.clear_search_cache"):
            updated = update_file(session, f.id, {"parent_id": test_folder.id})
        assert updated.parent_id == test_folder.id

    def test_update_nonexistent(self, session: Session) -> None:
        with pytest.raises(ResourceNotFoundError):
            update_file(session, 99999, {"name": "x"})


class TestDeleteFile:
    def test_delete_existing(self, session: Session, test_workspace: Workspace, test_user: User) -> None:
        f = File(name="del.txt", file_path="del.txt", file_size=10,
                 workspace_id=test_workspace.id, uploader_id=test_user.id)
        session.add(f)
        session.flush()
        file_id = f.id

        with patch("app.features.file.deletion.change_log_service"), \
             patch("app.features.file.deletion.clear_search_cache"):
            delete_file(session, file_id)
        assert session.get(File, file_id) is None

    def test_delete_nonexistent(self, session: Session) -> None:
        with pytest.raises(ResourceNotFoundError):
            delete_file(session, 99999)

    def test_delete_no_commit(self, session: Session, test_workspace: Workspace, test_user: User) -> None:
        f = File(name="nc.txt", file_path="nc.txt", file_size=10,
                 workspace_id=test_workspace.id, uploader_id=test_user.id)
        session.add(f)
        session.flush()
        file_id = f.id

        delete_file(session, file_id, commit=False, log_event=False)
        # 未 commit 但 session 中已标记删除


# 目录浏览


class TestGetFilesAndFolders:
    def test_basic_listing(self, session: Session, test_workspace: Workspace, test_user: User, test_folder: Folder) -> None:
        root = session.query(Folder).filter_by(
            workspace_id=test_workspace.id, parent_id=None
        ).first()
        assert root is not None

        # 添加文件到根目录
        f = File(name="file1.txt", file_path="f1.txt", file_size=100,
                 workspace_id=test_workspace.id, uploader_id=test_user.id,
                 parent_id=root.id)
        session.add(f)
        session.flush()

        result = get_files_and_folders(session, GetFilesAndFoldersParams(workspace_id=test_workspace.id, parent_id=root.id))
        assert "folders" in result
        assert "files" in result
        files_data = cast(dict[str, Any], result["files"])
        assert cast(int, files_data["total"]) >= 1

    def test_name_filter(self, session: Session, test_workspace: Workspace, test_user: User) -> None:
        root = session.query(Folder).filter_by(
            workspace_id=test_workspace.id, parent_id=None
        ).first()
        assert root is not None
        f = File(name="report_2024.pdf", file_path="r.pdf", file_size=100,
                 workspace_id=test_workspace.id, uploader_id=test_user.id,
                 parent_id=root.id)
        session.add(f)
        session.flush()

        result = get_files_and_folders(session, GetFilesAndFoldersParams(workspace_id=test_workspace.id, parent_id=root.id, name="report"))
        files_data = cast(dict[str, Any], result["files"])
        assert cast(int, files_data["total"]) >= 1

    def test_sort_by_name(self, session: Session, test_workspace: Workspace, test_user: User) -> None:
        root = session.query(Folder).filter_by(
            workspace_id=test_workspace.id, parent_id=None
        ).first()
        assert root is not None
        result = get_files_and_folders(session, GetFilesAndFoldersParams(workspace_id=test_workspace.id, parent_id=root.id, sort_by="name", order="asc"))
        assert result is not None

    def test_pagination(self, session: Session, test_workspace: Workspace, test_user: User) -> None:
        root = session.query(Folder).filter_by(
            workspace_id=test_workspace.id, parent_id=None
        ).first()
        assert root is not None
        for i in range(5):
            f = File(name=f"pg{i}.txt", file_path=f"pg{i}.txt", file_size=10,
                     workspace_id=test_workspace.id, uploader_id=test_user.id,
                     parent_id=root.id)
            session.add(f)
        session.flush()

        result = get_files_and_folders(session, GetFilesAndFoldersParams(workspace_id=test_workspace.id, parent_id=root.id, page=1, page_size=2))
        files_data = cast(dict[str, Any], result["files"])
        assert cast(int, files_data["pages"]) >= 3


# 搜索


class TestSearchFiles:
    async def test_fuzzy_search(self, session: Session, test_workspace: Workspace, test_user: User) -> None:
        f = File(name="searchable.txt", file_path="s.txt", file_size=10,
                 workspace_id=test_workspace.id, uploader_id=test_user.id, status="success")
        session.add(f)
        session.flush()

        result = await search_files(session, test_workspace.id, SearchFilesParams(query="searchable"))
        assert cast(int, result["total"]) >= 1

    async def test_empty_query(self, session: Session, test_workspace: Workspace, test_user: User) -> None:
        result = await search_files(session, test_workspace.id, SearchFilesParams(query=""))
        assert result["total"] == 0


# 上传预检 / 秒传


class TestPreflightUpload:
    def test_missing_filename(self, session: Session, test_workspace: Workspace, test_user: User) -> None:
        with pytest.raises(BusinessRuleError):
            get_upload_service().preflight_file_upload(session, test_workspace.id, test_user.id, cast(PreflightData, {
                "filename": "", "total_size": 100, "content_hash": "a" * 64
            }))

    def test_missing_size(self, session: Session, test_workspace: Workspace, test_user: User) -> None:
        with pytest.raises(BusinessRuleError):
            get_upload_service().preflight_file_upload(session, test_workspace.id, test_user.id, cast(PreflightData, {
                "filename": "f.txt", "total_size": 0, "content_hash": "a" * 64
            }))

    def test_malformed_content_hash(self, session: Session, test_workspace: Workspace, test_user: User) -> None:
        service = get_upload_service()
        with pytest.raises(BusinessRuleError):
            service.preflight_file_upload(session, test_workspace.id, test_user.id, cast(PreflightData, {
                "filename": "f.txt", "total_size": 100, "content_hash": "short"
            }))
        with pytest.raises(BusinessRuleError):
            service.preflight_file_upload(session, test_workspace.id, test_user.id, cast(PreflightData, {
                "filename": "f.txt", "total_size": 100, "content_hash": None
            }))

    def test_no_reusable_source(self, session: Session, test_workspace: Workspace, test_user: User) -> None:
        result = get_upload_service().preflight_file_upload(session, test_workspace.id, test_user.id, cast(PreflightData, {
            "filename": "new.txt", "total_size": 100, "content_hash": "b" * 64
        }))
        assert result["instant_upload"] is False


# 分片上传


class TestMultipartUpload:
    def test_init_invalid_chunk_size(self, session: Session, test_workspace: Workspace, test_user: User) -> None:
        with pytest.raises(BusinessRuleError):
            get_multipart_service().init_multipart_upload(session, test_workspace.id, test_user.id, cast(MultipartInitData, {
                "filename": "f.txt", "total_size": 100, "chunk_size": -1
            }))

    def test_unsafe_upload_id_rejected(self, session: Session, test_workspace: Workspace, test_user: User) -> None:
        with pytest.raises(BusinessRuleError):
            get_multipart_service().init_multipart_upload(session, test_workspace.id, test_user.id, cast(MultipartInitData, {
                "filename": "f.txt", "total_size": 100, "upload_id": "invalid/id"
            }))

    def test_init_and_status(self, session: Session, test_workspace: Workspace, test_user: User, tmp_path: Path) -> None:
        with patch("app.features.file.upload_config.UPLOAD_FOLDER", str(tmp_path)):
            service = get_multipart_service()
            result = service.init_multipart_upload(session, test_workspace.id, test_user.id, cast(MultipartInitData, {
                "filename": "big.bin", "total_size": 1024, "chunk_size": 512
            }))
            upload_id = cast(str, result["upload_id"])
            assert upload_id is not None
            assert result["total_chunks"] == 2
            assert result["instant_upload"] is False

            # 查询状态
            status = service.get_multipart_upload_status(
                session, test_workspace.id, test_user.id, upload_id,
            )
        assert status["total_chunks"] == 2
        assert status["uploaded_chunks"] == []
        assert (tmp_path / ".multipart" / str(test_workspace.id) / upload_id).is_dir()

    def test_abort(self, session: Session, test_workspace: Workspace, test_user: User, tmp_path: Path) -> None:
        with patch("app.features.file.upload_config.UPLOAD_FOLDER", str(tmp_path)):
            service = get_multipart_service()
            result = service.init_multipart_upload(session, test_workspace.id, test_user.id, cast(MultipartInitData, {
                "filename": "abort.bin", "total_size": 100, "chunk_size": 100
            }))
            upload_id = cast(str, result["upload_id"])
            service.abort_multipart_upload(
                session, test_workspace.id, test_user.id, upload_id,
            )
            # 中止后查询应报错
            with pytest.raises(ConflictError):
                service.get_multipart_upload_status(
                    session, test_workspace.id, test_user.id, upload_id,
                )
        assert not (tmp_path / ".multipart" / str(test_workspace.id) / upload_id).exists()


# 文件上传（mock 文件系统）


class TestCreateFile:
    def test_create_file(self, session: Session, test_workspace: Workspace, test_user: User, tmp_path: Path, mock_object_storage: Any) -> None:
        upload = _upload("test.txt", b"hello", "text/plain")

        with patch("app.features.file.upload_config.UPLOAD_FOLDER", str(tmp_path)):
            f = get_upload_service().create_uploaded_file(
                session, test_workspace.id, test_user.id, upload,
            )
        assert f.id is not None
        assert f.name == "test.txt"
        assert f.file_size == 5
        stored = session.get(File, f.id)
        assert stored is not None
        assert stored.name == "test.txt"
        assert mock_object_storage.objects[f.file_path] == b"hello"

    def test_object_key_sanitizes_filename(self, session: Session, test_workspace: Workspace, test_user: User, tmp_path: Path) -> None:
        upload = _upload("../../etc/passwd", b"x", "application/octet-stream")

        with patch("app.features.file.upload_config.UPLOAD_FOLDER", str(tmp_path)):
            f = get_upload_service().create_uploaded_file(
                session, test_workspace.id, test_user.id, upload,
            )
        assert ".." not in f.file_path
        assert "/" not in f.file_path
        assert f.file_path.endswith("etc_passwd")

    def test_mime_type_guessed_from_extension(self, session: Session, test_workspace: Workspace, test_user: User, tmp_path: Path) -> None:
        upload = _upload("photo.jpg", b"jpeg", None)

        with patch("app.features.file.upload_config.UPLOAD_FOLDER", str(tmp_path)):
            f = get_upload_service().create_uploaded_file(
                session, test_workspace.id, test_user.id, upload,
            )
        assert f.mime_type == "image/jpeg"

    def test_create_uploaded_file_no_filename(self, session: Session, test_workspace: Workspace, test_user: User) -> None:
        upload = FastAPIUploadAdapter(UploadFile(filename="", file=BytesIO()))
        with pytest.raises(BusinessRuleError):
            get_upload_service().create_uploaded_file(
                session, test_workspace.id, test_user.id, upload,
            )


class TestBatchCreateFiles:
    def test_create_uploaded_files(self, session: Session, test_workspace: Workspace, test_user: User, tmp_path: Path) -> None:
        uploads: list[UploadSource] = [
            _upload("b1.txt", b"hello", "text/plain"),
            _upload("b2.txt", b"world", "text/plain"),
        ]

        with patch("app.features.file.upload_config.UPLOAD_FOLDER", str(tmp_path)):
            files = get_upload_service().create_uploaded_files(
                session, test_workspace.id, test_user.id, uploads,
            )
        assert [f.name for f in files] == ["b1.txt", "b2.txt"]
        assert [f.file_size for f in files] == [5, 5]
        assert len({f.file_path for f in files}) == 2
        f0 = session.get(File, files[0].id)
        assert f0 is not None
        assert f0.name == "b1.txt"

    def test_create_uploaded_files_no_valid(self, session: Session, test_workspace: Workspace, test_user: User) -> None:
        upload = FastAPIUploadAdapter(UploadFile(filename="", file=BytesIO()))
        uploads: list[UploadSource] = [upload]
        with pytest.raises(BusinessRuleError):
            get_upload_service().create_uploaded_files(
                session, test_workspace.id, test_user.id, uploads,
            )


# 批量删除 / 状态


class TestBatchDeleteAndStatus:
    def test_get_all_files(self, session: Session, test_workspace: Workspace, test_user: User) -> None:
        f = File(name="all.txt", file_path="all.txt", file_size=10,
                 workspace_id=test_workspace.id, uploader_id=test_user.id)
        session.add(f)
        session.flush()

        files = get_all_files(session, test_workspace.id)
        assert len(files) >= 1

    async def test_process_status(self, session: Session, test_workspace: Workspace, test_user: User) -> None:
        session.add(File(name="s1.txt", file_path="s1.txt", file_size=10,
                         workspace_id=test_workspace.id, status="success"))
        session.add(File(name="s2.txt", file_path="s2.txt", file_size=10,
                         workspace_id=test_workspace.id, status="pending"))
        session.flush()

        status = await process_status(session, test_workspace.id)
        assert "成功" in status
        assert status["成功"] >= 1

