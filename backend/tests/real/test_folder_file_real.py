"""SQLite-backed folder/file metadata, cleanup, and search behavior tests.

The remote vector-search scenario is opt-in via ``TEST_AI_API=1``.
"""

import os
import time

import pytest

from tests.real.conftest import skip_unless_ai_api

from app.exceptions import PermissionDeniedError, ResourceNotFoundError
from app.features.file.deletion import delete_file
from app.features.file.search_service import _search_files_fuzzy
from app.features.file.service import (
    cleanup_expired_uploads,
    get_all_files,
    get_authorized_file,
    get_downloadable_file,
    get_file,
    get_files_and_folders,
    GetFilesAndFoldersParams,
    get_root_file_id,
    process_status,
    rebuild_failed_indexes,
    retry_embedding,
    update_file,
)
from app.features.folder.service import (
    create_folder,
    delete_folder,
    get_authorized_folder,
    get_folder,
    get_folders,
    get_root_folder_id,
    update_folder,
)
from app.infra.extensions import UPLOAD_FOLDER
from app.models.file import File
from app.models.folder import Folder


# ===========================================================================
# folder/service.py
# ===========================================================================


class TestFolderService:
    """文件夹 CRUD 与缓存。"""

    def test_create_folder(self, session, test_workspace):
        """创建文件夹。"""
        folder = create_folder(session, {
            "name": "新文件夹", "workspace_id": test_workspace.id, "parent_id": None
        })
        assert folder.id is not None
        assert folder.name == "新文件夹"

    def test_create_folder_exception_rollback(self, session):
        """创建失败时回滚（缺少必填字段）。"""
        with pytest.raises(Exception):
            create_folder(session, {"workspace_id": 99999})  # 缺少 name

    def test_get_folder_success(self, session, test_folder):
        """获取存在的文件夹。"""
        folder = get_folder(session, test_folder.id)
        assert folder.name == "文档"

    def test_get_folder_not_found(self, session):
        """不存在抛 404。"""
        with pytest.raises(ResourceNotFoundError):
            get_folder(session, 99999)

    def test_get_authorized_folder_success(self, session, test_workspace, test_user, test_folder):
        """同空间文件夹通过校验。"""
        folder = get_authorized_folder(session, test_workspace.id, test_user.id, test_folder.id)
        assert folder.id == test_folder.id

    def test_get_authorized_folder_wrong_workspace(self, session, test_user, test_folder):
        """跨空间访问被拒绝。"""
        with pytest.raises(PermissionDeniedError):
            get_authorized_folder(session, 99999, test_user.id, test_folder.id)

    def test_update_folder(self, session, test_folder):
        """更新文件夹名称。"""
        updated = update_folder(session, test_folder.id, {"name": "重命名"})
        assert updated.name == "重命名"

    def test_update_folder_not_found(self, session):
        """更新不存在抛 404。"""
        with pytest.raises(ResourceNotFoundError):
            update_folder(session, 99999, {"name": "x"})

    def test_delete_folder_recursive(self, session, test_workspace, test_folder):
        """递归删除文件夹及子文件。"""
        # 在文件夹下创建文件
        f = File(name="a.txt", file_path="a.txt", file_size=10,
                 workspace_id=test_workspace.id, parent_id=test_folder.id)
        session.add(f)
        # 创建子文件夹
        sub = Folder(name="子目录", workspace_id=test_workspace.id, parent_id=test_folder.id)
        session.add(sub)
        session.commit()
        folder_id = test_folder.id
        delete_folder(session, folder_id)
        assert session.get(Folder, folder_id) is None
        assert session.get(Folder, sub.id) is None

    def test_delete_folder_not_found(self, session):
        """删除不存在抛 404。"""
        with pytest.raises(ResourceNotFoundError):
            delete_folder(session, 99999)

    def test_get_root_folder_id(self, session, test_workspace):
        """获取根文件夹 ID。"""
        root_id = get_root_folder_id(session, test_workspace.id)
        assert root_id is not None
        root = session.get(Folder, root_id)
        assert root.name == "/"
        assert root.parent_id is None

    def test_get_root_folder_id_none(self, session):
        """无根文件夹时返回 None。"""
        result = get_root_folder_id(session, 99999)
        assert result is None

    def test_get_folders(self, session, test_workspace, test_folder):
        """获取空间全部文件夹。"""
        folders = get_folders(session, test_workspace.id)
        assert len(folders) >= 2  # 根 + 文档


# ===========================================================================
# file/service.py — 工具函数
# ===========================================================================


# ===========================================================================
# file/service.py — CRUD
# ===========================================================================


class TestFileServiceCRUD:
    """文件 CRUD 操作。"""

    def test_get_file_success(self, session, test_workspace, test_user):
        """获取存在的文件。"""
        f = File(name="a.txt", file_path="a.txt", file_size=1,
                 workspace_id=test_workspace.id, uploader_id=test_user.id)
        session.add(f)
        session.commit()
        result = get_file(session, f.id)
        assert result.name == "a.txt"

    def test_get_file_not_found(self, session):
        """不存在抛 404。"""
        with pytest.raises(ResourceNotFoundError):
            get_file(session, 99999)

    def test_get_authorized_file_success(self, session, test_workspace, test_user):
        """同空间文件通过校验。"""
        f = File(name="b.txt", file_path="b.txt", file_size=1,
                 workspace_id=test_workspace.id, uploader_id=test_user.id)
        session.add(f)
        session.commit()
        result = get_authorized_file(session, test_workspace.id, test_user.id, f.id)
        assert result.id == f.id

    def test_get_authorized_file_wrong_workspace(self, session, test_workspace, test_user):
        """跨空间访问被拒绝。"""
        f = File(name="c.txt", file_path="c.txt", file_size=1,
                 workspace_id=test_workspace.id, uploader_id=test_user.id)
        session.add(f)
        session.commit()
        with pytest.raises(PermissionDeniedError):
            get_authorized_file(session, 99999, test_user.id, f.id)

    def test_get_downloadable_file_missing_disk(self, session, test_workspace, test_user):
        """磁盘文件不存在抛 404。"""
        f = File(name="d.txt", file_path="nonexist_disk.txt", file_size=1,
                 workspace_id=test_workspace.id, uploader_id=test_user.id)
        session.add(f)
        session.commit()
        with pytest.raises(ResourceNotFoundError, match="not found on server"):
            get_downloadable_file(session, test_workspace.id, test_user.id, f.id)

    def test_update_file_rename(self, session, test_workspace, test_user):
        """重命名文件。"""
        f = File(name="old.txt", file_path="old.txt", file_size=1,
                 workspace_id=test_workspace.id, uploader_id=test_user.id)
        session.add(f)
        session.commit()
        updated = update_file(session, f.id, {"name": "new.txt"})
        assert updated.name == "new.txt"

    def test_update_file_move(self, session, test_workspace, test_user, test_folder):
        """移动文件。"""
        f = File(name="mv.txt", file_path="mv.txt", file_size=1,
                 workspace_id=test_workspace.id, uploader_id=test_user.id)
        session.add(f)
        session.commit()
        updated = update_file(session, f.id, {"parent_id": test_folder.id})
        assert updated.parent_id == test_folder.id

    def test_update_file_not_found(self, session):
        """更新不存在抛 404。"""
        with pytest.raises(ResourceNotFoundError):
            update_file(session, 99999, {"name": "x"})

    def test_delete_file_no_physical(self, session, test_workspace, test_user):
        """删除文件记录（磁盘文件不存在时不报错）。"""
        f = File(name="del.txt", file_path="del_nonexist.txt", file_size=1,
                 workspace_id=test_workspace.id, uploader_id=test_user.id)
        session.add(f)
        session.commit()
        fid = f.id
        delete_file(session, fid)
        assert session.get(File, fid) is None

    def test_delete_file_with_physical(self, session, test_workspace, test_user):
        """删除文件记录 + 物理文件。"""
        # 创建真实磁盘文件
        os.makedirs(UPLOAD_FOLDER, exist_ok=True)
        path = os.path.join(UPLOAD_FOLDER, "phys_test.txt")
        with open(path, "w") as fp:
            fp.write("data")
        f = File(name="phys.txt", file_path="phys_test.txt", file_size=4,
                 workspace_id=test_workspace.id, uploader_id=test_user.id)
        session.add(f)
        session.commit()
        fid = f.id
        delete_file(session, fid)
        assert session.get(File, fid) is None
        assert not os.path.exists(path)

    def test_delete_file_shared_path_keeps_physical(self, session, test_workspace, test_user):
        """秒传共享路径：有其它引用时不删物理文件。"""
        os.makedirs(UPLOAD_FOLDER, exist_ok=True)
        path = os.path.join(UPLOAD_FOLDER, "shared_phys.txt")
        with open(path, "w") as fp:
            fp.write("shared")
        f1 = File(name="s1.txt", file_path="shared_phys.txt", file_size=6,
                  workspace_id=test_workspace.id, uploader_id=test_user.id)
        f2 = File(name="s2.txt", file_path="shared_phys.txt", file_size=6,
                  workspace_id=test_workspace.id, uploader_id=test_user.id)
        session.add_all([f1, f2])
        session.commit()
        delete_file(session, f1.id)
        # 物理文件仍在（f2 还引用）
        assert os.path.exists(path)
        # 清理
        delete_file(session, f2.id)

    def test_delete_file_not_found(self, session):
        """删除不存在抛 404。"""
        with pytest.raises(ResourceNotFoundError):
            delete_file(session, 99999)

    def test_get_all_files(self, session, test_workspace, test_user):
        """获取空间全部文件。"""
        session.add(File(name="x.txt", file_path="x.txt", file_size=1,
                         workspace_id=test_workspace.id))
        session.commit()
        files = get_all_files(session, test_workspace.id)
        assert len(files) >= 1

    @pytest.mark.asyncio
    async def test_process_status(self, session, test_workspace, test_user):
        """按状态聚合统计。"""
        session.add(File(name="p.txt", file_path="p.txt", file_size=1,
                         workspace_id=test_workspace.id, status="pending"))
        session.add(File(name="s.txt", file_path="s.txt", file_size=1,
                         workspace_id=test_workspace.id, status="success"))
        session.add(File(name="f.txt", file_path="f.txt", file_size=1,
                         workspace_id=test_workspace.id, status="fail"))
        session.commit()
        stats = await process_status(session, test_workspace.id)
        assert stats["处理中"] >= 1
        assert stats["成功"] >= 1
        assert stats["失败"] >= 1


# ===========================================================================
# file/service.py — 搜索与列表
# ===========================================================================


class TestFileServiceSearch:
    """文件搜索与列表。"""

    def test_get_files_and_folders_basic(self, session, test_workspace, test_user, test_folder):
        """基本列表（文件夹 + 文件混合分页）。"""
        root_id = get_root_folder_id(session, test_workspace.id)
        for i in range(3):
            session.add(File(name=f"file{i}.txt", file_path=f"f{i}.txt", file_size=i * 10,
                             workspace_id=test_workspace.id, parent_id=root_id))
        session.commit()
        result = get_files_and_folders(session, GetFilesAndFoldersParams(workspace_id=test_workspace.id, parent_id=root_id, page=1, page_size=10))
        assert "folders" in result
        assert "files" in result
        assert result["files"]["total"] >= 3

    def test_get_files_and_folders_page_correction(self, session, test_workspace):
        """page < 1 时修正为 1。"""
        root_id = get_root_folder_id(session, test_workspace.id)
        result = get_files_and_folders(session, GetFilesAndFoldersParams(workspace_id=test_workspace.id, parent_id=root_id, page=0))
        assert result["files"]["page"] == 1

    def test_get_files_and_folders_sort_by_name(self, session, test_workspace, test_user):
        """按名称排序。"""
        root_id = get_root_folder_id(session, test_workspace.id)
        session.add(File(name="zzz.txt", file_path="z.txt", file_size=1,
                         workspace_id=test_workspace.id, parent_id=root_id))
        session.add(File(name="aaa.txt", file_path="a.txt", file_size=1,
                         workspace_id=test_workspace.id, parent_id=root_id))
        session.commit()
        result = get_files_and_folders(session, GetFilesAndFoldersParams(workspace_id=test_workspace.id, parent_id=root_id, sort_by="name", order="asc"))
        names = [f["name"] for f in result["files"]["items"]]
        if len(names) >= 2:
            assert names[0] <= names[1]

    def test_get_files_and_folders_filter_name(self, session, test_workspace, test_user):
        """按名称过滤。"""
        root_id = get_root_folder_id(session, test_workspace.id)
        session.add(File(name="unique_xyz.txt", file_path="ux.txt", file_size=1,
                         workspace_id=test_workspace.id, parent_id=root_id))
        session.commit()
        result = get_files_and_folders(session, GetFilesAndFoldersParams(workspace_id=test_workspace.id, parent_id=root_id, name="unique_xyz"))
        assert result["files"]["total"] >= 1

    def test_search_files_fuzzy(self, session, test_workspace, test_user):
        """模糊搜索。"""
        session.add(File(name="searchable_doc.pdf", file_path="sd.pdf", file_size=1,
                         workspace_id=test_workspace.id))
        session.commit()
        result = _search_files_fuzzy(session, test_workspace.id, "searchable", 1, 10)
        assert result["total"] >= 1

    def test_search_files_fuzzy_no_match(self, session, test_workspace):
        """无匹配返回空。"""
        result = _search_files_fuzzy(session, test_workspace.id, "zzz_nonexist_zzz", 1, 10)
        assert result["total"] == 0

    def test_get_root_file_id_none(self, session, test_workspace, real_redis):
        """无根文件返回 None。"""
        result = get_root_file_id(session, test_workspace.id)
        assert result is None


# ===========================================================================
# file/service.py — 分片上传
# ===========================================================================



# ===========================================================================
# file/service.py — 重建索引与清理
# ===========================================================================


class TestFileServiceMaintenance:
    """文件维护：重建索引、清理过期上传。"""

    def test_rebuild_failed_indexes_no_failed(self, session, test_workspace):
        """无失败文件返回 0。"""
        result = rebuild_failed_indexes(session, test_workspace.id)
        assert result == 0

    def test_rebuild_failed_indexes_with_failed(self, session, test_workspace):
        failed = File(
            name="fail.txt",
            file_path="fail.txt",
            file_size=1,
            workspace_id=test_workspace.id,
            status="fail",
        )
        session.add(failed)
        session.commit()

        result = rebuild_failed_indexes(session, test_workspace.id)

        assert result == 1
        session.refresh(failed)
        assert failed.status == "pending"

    def test_rebuild_failed_indexes_db_error(self, session, test_workspace, test_user):
        from app.exceptions import ServiceOperationError

        session.add(
            File(
                name="fail.txt",
                file_path="fail.txt",
                file_size=1,
                workspace_id=test_workspace.id,
                uploader_id=test_user.id,
                status="fail",
            ),
        )
        session.commit()
        session.add(
            File(
                name="bad.txt",
                file_path="bad.txt",
                file_size=1,
                workspace_id=99999,
                uploader_id=test_user.id,
                status="pending",
            ),
        )

        with pytest.raises(ServiceOperationError):
            rebuild_failed_indexes(session, test_workspace.id)

    def test_retry_embedding_file_not_found(self, session):
        """文件不存在时静默返回。"""
        retry_embedding(session, 99999)  # 不抛异常

    def test_cleanup_expired_uploads_no_dir(self, tmp_path, monkeypatch):
        upload_root = tmp_path / "nonexistent"
        monkeypatch.setattr(
            "app.features.file.upload_config.UPLOAD_FOLDER", str(upload_root),
        )
        assert cleanup_expired_uploads() == 0

    def test_cleanup_expired_uploads_removes_old(self, tmp_path, monkeypatch):
        upload_root = tmp_path / "uploads"
        mp_root = upload_root / ".multipart"
        upload_dir = mp_root / "1" / "old_upload_id_123456789"
        upload_dir.mkdir(parents=True)
        old_time = time.time() - 48 * 3600
        os.utime(upload_dir, (old_time, old_time))
        monkeypatch.setattr(
            "app.features.file.upload_config.UPLOAD_FOLDER", str(upload_root),
        )

        count = cleanup_expired_uploads(max_age_hours=24)

        assert count == 1
        assert not upload_dir.exists()

    def test_cleanup_expired_uploads_keeps_recent(self, tmp_path, monkeypatch):
        upload_root = tmp_path / "uploads"
        upload_dir = upload_root / ".multipart" / "1" / "recent_upload_id_12345678"
        upload_dir.mkdir(parents=True)
        monkeypatch.setattr(
            "app.features.file.upload_config.UPLOAD_FOLDER", str(upload_root),
        )

        count = cleanup_expired_uploads(max_age_hours=24)

        assert count == 0
        assert upload_dir.exists()
# ===========================================================================
# Tests requiring the external embedding provider
# ===========================================================================


class TestFileServiceExternalDeps:
    """Cases that need a real remote embedding provider."""
    @skip_unless_ai_api
    def test_search_files_vector_success(self, session, test_workspace):
        """向量搜索完整流程 (L902-925)。

        外部依赖：Embedding API (OpenAI 兼容接口)
        原因：embedding_desc() 调用远程 AI 服务生成查询向量
        影响分支: search_service.search_files 的向量分支
        环境变量：TEST_AI_API=1 启用
        """
        import asyncio
        from app.features.file.search_service import SearchFilesParams, search_files

        result = asyncio.run(search_files(
            session,
            test_workspace.id,
            SearchFilesParams(query="测试查询", search_type="vector"),
        ))
        assert "items" in result
