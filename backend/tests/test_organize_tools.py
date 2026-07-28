"""organize/tools.py 单元测试。

工具函数内部使用 get_session() → SessionLocal()，需 mock SessionLocal 让它使用测试 session。
"""
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_organize_session(session, test_workspace):
    """让 organize tools 的 get_session() 返回测试 session。"""
    with patch("app.features.folder.organize.tools.get_session", return_value=session), \
         patch("app.features.folder.organize.tools.SessionLocal", return_value=session), \
         patch("app.features.folder.organize.tools.clear_workspace_cache"):
        yield session


class TestClearWorkspaceCache:
    def test_clear_cache_success(self):
        from app.features.folder.organize.tools import clear_workspace_cache
        mock_redis = MagicMock()
        mock_redis.scan.return_value = (0, [])
        with patch("app.features.folder.organize.tools.redis_client", mock_redis):
            clear_workspace_cache(1)
        mock_redis.delete.assert_called_once_with("workspace:folders:1")

    def test_clear_cache_with_search_keys(self):
        from app.features.folder.organize.tools import clear_workspace_cache
        mock_redis = MagicMock()
        mock_redis.scan.side_effect = [(1, ["search:a:1:x"]), (0, [])]
        with patch("app.features.folder.organize.tools.redis_client", mock_redis):
            clear_workspace_cache(1)
        mock_redis.delete.assert_any_call("workspace:folders:1")
        mock_redis.delete.assert_any_call("search:a:1:x")

    def test_clear_cache_exception(self):
        from app.features.folder.organize.tools import clear_workspace_cache
        with patch("app.features.folder.organize.tools.redis_client", side_effect=Exception("Redis down")):
            # 不应抛出
            clear_workspace_cache(1)


class TestCheckMixedFolders:
    def test_clean_workspace(self, session, test_workspace):
        from app.features.folder.organize.tools import _check_mixed_folders
        is_clean, mixed = _check_mixed_folders(session, test_workspace.id)
        assert is_clean is True
        assert mixed == []

    def test_mixed_root(self, session, test_workspace):
        from app.features.folder.organize.tools import _check_mixed_folders
        from app.models.file import File
        from app.models.folder import Folder

        # 根目录同时有子文件夹和文件
        folder = Folder(name="sub", workspace_id=test_workspace.id, parent_id=None)
        session.add(folder)
        f = File(name="a.txt", file_path="hash.txt", file_size=10, mime_type="text/plain",
                 workspace_id=test_workspace.id, uploader_id=1, parent_id=None,
                 content_hash="a" * 64, status="success")
        session.add(f)
        session.flush()

        is_clean, mixed = _check_mixed_folders(session, test_workspace.id)
        assert is_clean is False
        assert any(m["id"] == 0 for m in mixed)

    def test_mixed_subfolder(self, session, test_workspace, test_folder):
        from app.features.folder.organize.tools import _check_mixed_folders
        from app.models.file import File

        # test_folder 既有子文件夹又有文件
        from app.models.folder import Folder
        sub = Folder(name="sub2", workspace_id=test_workspace.id, parent_id=test_folder.id)
        session.add(sub)
        f = File(name="b.txt", file_path="hash2.txt", file_size=10, mime_type="text/plain",
                 workspace_id=test_workspace.id, uploader_id=1, parent_id=test_folder.id,
                 content_hash="b" * 64, status="success")
        session.add(f)
        session.flush()

        is_clean, mixed = _check_mixed_folders(session, test_workspace.id)
        assert is_clean is False
        assert any(m["id"] == test_folder.id for m in mixed)


class TestCheckEmptyFolders:
    def test_no_empty(self, session, test_workspace, test_folder):
        from app.features.folder.organize.tools import _check_empty_folders
        from app.models.file import File

        f = File(name="c.txt", file_path="hash3.txt", file_size=10, mime_type="text/plain",
                 workspace_id=test_workspace.id, uploader_id=1, parent_id=test_folder.id,
                 content_hash="c" * 64, status="success")
        session.add(f)
        session.flush()

        is_clean, empties = _check_empty_folders(session, test_workspace.id)
        assert is_clean is True

    def test_has_empty(self, session, test_workspace, test_folder):
        from app.features.folder.organize.tools import _check_empty_folders

        is_clean, empties = _check_empty_folders(session, test_workspace.id)
        assert is_clean is False
        assert any(e["id"] == test_folder.id for e in empties)


class TestInternalChecks:
    def test_check_mixed_folders_internal_clean(self, session, test_workspace):
        from app.features.folder.organize.tools import check_mixed_folders_internal
        with patch("app.features.folder.organize.tools.get_session", return_value=session):
            is_clean, msg = check_mixed_folders_internal(test_workspace.id)
        assert is_clean is True
        assert "未发现" in msg

    def test_check_mixed_folders_internal_exception(self):
        from app.features.folder.organize.tools import check_mixed_folders_internal
        mock_session = MagicMock()
        mock_session.query.side_effect = Exception("DB error")
        with patch("app.features.folder.organize.tools.get_session", return_value=mock_session):
            is_clean, msg = check_mixed_folders_internal(1)
        assert is_clean is False
        assert "检查失败" in msg

    def test_check_empty_folders_internal_clean(self, session, test_workspace, test_folder):
        from app.features.folder.organize.tools import check_empty_folders_internal
        from app.models.file import File
        f = File(name="d.txt", file_path="hash4.txt", file_size=10, mime_type="text/plain",
                 workspace_id=test_workspace.id, uploader_id=1, parent_id=test_folder.id,
                 content_hash="d" * 64, status="success")
        session.add(f)
        session.flush()
        with patch("app.features.folder.organize.tools.get_session", return_value=session):
            is_clean, msg = check_empty_folders_internal(test_workspace.id)
        assert is_clean is True

    def test_check_empty_folders_internal_exception(self):
        from app.features.folder.organize.tools import check_empty_folders_internal
        mock_session = MagicMock()
        mock_session.query.side_effect = Exception("DB error")
        with patch("app.features.folder.organize.tools.get_session", return_value=mock_session):
            is_clean, msg = check_empty_folders_internal(1)
        assert is_clean is False
        assert "检查失败" in msg


class TestGetAllFiles:
    def test_get_all_files_empty(self, session, test_workspace):
        from app.features.folder.organize.tools import get_all_files
        with patch("app.features.folder.organize.tools.get_session", return_value=session):
            result = get_all_files.invoke({"workspace_id": test_workspace.id})
        assert "没有找到文件" in result

    def test_get_all_files_with_data(self, session, test_workspace):
        from app.features.folder.organize.tools import get_all_files
        from app.models.file import File
        f = File(name="test.txt", file_path="h.txt", file_size=10, mime_type="text/plain",
                 workspace_id=test_workspace.id, uploader_id=1, parent_id=None,
                 content_hash="e" * 64, status="success")
        session.add(f)
        session.flush()
        with patch("app.features.folder.organize.tools.get_session", return_value=session):
            result = get_all_files.invoke({"workspace_id": test_workspace.id})
        assert "test.txt" in result

    def test_get_all_files_exception(self):
        from app.features.folder.organize.tools import get_all_files
        mock_session = MagicMock()
        mock_session.query.side_effect = Exception("DB error")
        with patch("app.features.folder.organize.tools.get_session", return_value=mock_session):
            result = get_all_files.invoke({"workspace_id": 1})
        assert "获取文件列表失败" in result


class TestGetFolderTree:
    def test_get_folder_tree(self, session, test_workspace, test_folder):
        from app.features.folder.organize.tools import get_folder_tree
        with patch("app.features.folder.organize.tools.get_session", return_value=session):
            result = get_folder_tree.invoke({"workspace_id": test_workspace.id})
        assert "文档" in result

    def test_get_folder_tree_exception(self):
        from app.features.folder.organize.tools import get_folder_tree
        mock_session = MagicMock()
        mock_session.query.side_effect = Exception("DB error")
        with patch("app.features.folder.organize.tools.get_session", return_value=mock_session):
            result = get_folder_tree.invoke({"workspace_id": 1})
        assert "获取文件夹结构失败" in result


class TestCreateFolder:
    def test_create_folder_new(self, session, test_workspace):
        from app.features.folder.organize.tools import create_folder
        with patch("app.features.folder.organize.tools.get_session", return_value=session), \
             patch("app.features.folder.organize.tools.clear_workspace_cache"):
            result = create_folder.invoke({
                "name": "newfolder", "parent_id": 0, "workspace_id": test_workspace.id
            })
        assert "已创建文件夹" in result

    def test_create_folder_existing(self, session, test_workspace, test_folder):
        from app.features.folder.organize.tools import create_folder
        # test_folder 的 parent_id 是 root.id，需要用相同的 parent_id 才能找到同名
        with patch("app.features.folder.organize.tools.get_session", return_value=session), \
             patch("app.features.folder.organize.tools.clear_workspace_cache"):
            result = create_folder.invoke({
                "name": "文档", "parent_id": test_folder.parent_id, "workspace_id": test_workspace.id
            })
        assert "已存在" in result

    def test_create_folder_exception(self):
        from app.features.folder.organize.tools import create_folder
        mock_session = MagicMock()
        mock_session.query.side_effect = Exception("DB error")
        with patch("app.features.folder.organize.tools.get_session", return_value=mock_session):
            result = create_folder.invoke({
                "name": "x", "parent_id": 0, "workspace_id": 1
            })
        assert "创建文件夹失败" in result


class TestRenameFolder:
    def test_rename_folder_success(self, session, test_workspace, test_folder):
        from app.features.folder.organize.tools import rename_folder
        with patch("app.features.folder.organize.tools.get_session", return_value=session), \
             patch("app.features.folder.organize.tools.clear_workspace_cache"):
            result = rename_folder.invoke({
                "folder_id": test_folder.id, "new_name": "renamed", "workspace_id": test_workspace.id
            })
        assert "重命名" in result

    def test_rename_folder_not_found(self, session, test_workspace):
        from app.features.folder.organize.tools import rename_folder
        with patch("app.features.folder.organize.tools.get_session", return_value=session):
            result = rename_folder.invoke({
                "folder_id": 99999, "new_name": "x", "workspace_id": test_workspace.id
            })
        assert "不存在" in result

    def test_rename_folder_wrong_workspace(self, session, test_workspace, test_folder):
        from app.features.folder.organize.tools import rename_folder
        with patch("app.features.folder.organize.tools.get_session", return_value=session):
            result = rename_folder.invoke({
                "folder_id": test_folder.id, "new_name": "x", "workspace_id": 99999
            })
        assert "权限错误" in result

    def test_rename_folder_duplicate(self, session, test_workspace, test_folder):
        from app.features.folder.organize.tools import rename_folder
        from app.models.folder import Folder
        f2 = Folder(name="dup", workspace_id=test_workspace.id, parent_id=test_folder.parent_id)
        session.add(f2)
        session.flush()
        with patch("app.features.folder.organize.tools.get_session", return_value=session):
            result = rename_folder.invoke({
                "folder_id": test_folder.id, "new_name": "dup", "workspace_id": test_workspace.id
            })
        assert "已存在" in result


class TestMoveFile:
    def test_move_file_success(self, session, test_workspace, test_folder):
        from app.features.folder.organize.tools import move_file
        from app.models.file import File
        f = File(name="mv.txt", file_path="mv.txt", file_size=10, mime_type="text/plain",
                 workspace_id=test_workspace.id, uploader_id=1, parent_id=None,
                 content_hash="f" * 64, status="success")
        session.add(f)
        session.flush()
        with patch("app.features.folder.organize.tools.get_session", return_value=session), \
             patch("app.features.folder.organize.tools.clear_workspace_cache"):
            result = move_file.invoke({
                "file_id": f.id, "target_folder_id": test_folder.id, "workspace_id": test_workspace.id
            })
        assert "移动" in result

    def test_move_file_not_found(self, session, test_workspace):
        from app.features.folder.organize.tools import move_file
        with patch("app.features.folder.organize.tools.get_session", return_value=session):
            result = move_file.invoke({
                "file_id": 99999, "target_folder_id": 0, "workspace_id": test_workspace.id
            })
        assert "不存在" in result

    def test_move_file_wrong_workspace(self, session, test_workspace, test_folder):
        from app.features.folder.organize.tools import move_file
        from app.models.file import File
        f = File(name="mv2.txt", file_path="mv2.txt", file_size=10, mime_type="text/plain",
                 workspace_id=test_workspace.id, uploader_id=1, parent_id=None,
                 content_hash="1" * 64, status="success")
        session.add(f)
        session.flush()
        with patch("app.features.folder.organize.tools.get_session", return_value=session):
            result = move_file.invoke({
                "file_id": f.id, "target_folder_id": 0, "workspace_id": 99999
            })
        assert "权限错误" in result

    def test_move_file_to_root(self, session, test_workspace, test_folder):
        from app.features.folder.organize.tools import move_file
        from app.models.file import File
        f = File(name="mvroot.txt", file_path="mvroot.txt", file_size=10, mime_type="text/plain",
                 workspace_id=test_workspace.id, uploader_id=1, parent_id=test_folder.id,
                 content_hash="2" * 64, status="success")
        session.add(f)
        session.flush()
        with patch("app.features.folder.organize.tools.get_session", return_value=session), \
             patch("app.features.folder.organize.tools.clear_workspace_cache"):
            result = move_file.invoke({
                "file_id": f.id, "target_folder_id": 0, "workspace_id": test_workspace.id
            })
        assert "移动" in result


class TestGetFileInformation:
    def test_get_file_info_success(self, session, test_workspace):
        from app.features.folder.organize.tools import get_file_information
        from app.models.file import File
        f = File(name="info.txt", file_path="info.txt", file_size=100, mime_type="text/plain",
                 workspace_id=test_workspace.id, uploader_id=1, parent_id=None,
                 content_hash="3" * 64, status="success", description="A test file")
        session.add(f)
        session.flush()
        with patch("app.features.folder.organize.tools.get_session", return_value=session):
            result = get_file_information.invoke({"file_id": f.id})
        assert "info.txt" in result

    def test_get_file_info_not_found(self, session):
        from app.features.folder.organize.tools import get_file_information
        with patch("app.features.folder.organize.tools.get_session", return_value=session):
            result = get_file_information.invoke({"file_id": 99999})
        assert "不存在" in result

    def test_get_file_info_long_description(self, session, test_workspace):
        from app.features.folder.organize.tools import get_file_information
        from app.models.file import File
        f = File(name="long.txt", file_path="long.txt", file_size=10, mime_type="text/plain",
                 workspace_id=test_workspace.id, uploader_id=1, parent_id=None,
                 content_hash="4" * 64, status="success",
                 description="x" * 300)
        session.add(f)
        session.flush()
        with patch("app.features.folder.organize.tools.get_session", return_value=session):
            result = get_file_information.invoke({"file_id": f.id})
        assert "..." in result


class TestDeleteFolder:
    def test_delete_folder_success(self, session, test_workspace):
        from app.features.folder.organize.tools import delete_folder
        from app.models.folder import Folder
        f = Folder(name="todelete", workspace_id=test_workspace.id, parent_id=None)
        session.add(f)
        session.flush()
        with patch("app.features.folder.organize.tools.get_session", return_value=session), \
             patch("app.features.folder.organize.tools.clear_workspace_cache"):
            result = delete_folder.invoke({
                "folder_id": f.id, "workspace_id": test_workspace.id
            })
        assert "已删除" in result

    def test_delete_folder_not_found(self, session, test_workspace):
        from app.features.folder.organize.tools import delete_folder
        with patch("app.features.folder.organize.tools.get_session", return_value=session):
            result = delete_folder.invoke({"folder_id": 99999, "workspace_id": test_workspace.id})
        assert "不存在" in result

    def test_delete_folder_not_empty_subfolders(self, session, test_workspace, test_folder):
        from app.features.folder.organize.tools import delete_folder
        from app.models.folder import Folder
        sub = Folder(name="sub3", workspace_id=test_workspace.id, parent_id=test_folder.id)
        session.add(sub)
        session.flush()
        with patch("app.features.folder.organize.tools.get_session", return_value=session):
            result = delete_folder.invoke({
                "folder_id": test_folder.id, "workspace_id": test_workspace.id
            })
        assert "不为空" in result and "子文件夹" in result

    def test_delete_folder_not_empty_files(self, session, test_workspace, test_folder):
        from app.features.folder.organize.tools import delete_folder
        from app.models.file import File
        f = File(name="infolder.txt", file_path="inf.txt", file_size=10, mime_type="text/plain",
                 workspace_id=test_workspace.id, uploader_id=1, parent_id=test_folder.id,
                 content_hash="5" * 64, status="success")
        session.add(f)
        session.flush()
        with patch("app.features.folder.organize.tools.get_session", return_value=session):
            result = delete_folder.invoke({
                "folder_id": test_folder.id, "workspace_id": test_workspace.id
            })
        assert "不为空" in result and "文件" in result

    def test_delete_folder_wrong_workspace(self, session, test_workspace, test_folder):
        from app.features.folder.organize.tools import delete_folder
        with patch("app.features.folder.organize.tools.get_session", return_value=session):
            result = delete_folder.invoke({
                "folder_id": test_folder.id, "workspace_id": 99999
            })
        assert "权限错误" in result


class TestMergeFolders:
    def test_merge_folders_success(self, session, test_workspace):
        from app.features.folder.organize.tools import merge_folders
        from app.models.folder import Folder
        src = Folder(name="src", workspace_id=test_workspace.id, parent_id=None)
        tgt = Folder(name="tgt", workspace_id=test_workspace.id, parent_id=None)
        session.add_all([src, tgt])
        session.flush()
        with patch("app.features.folder.organize.tools.get_session", return_value=session), \
             patch("app.features.folder.organize.tools.clear_workspace_cache"):
            result = merge_folders.invoke({
                "source_folder_id": src.id, "target_folder_id": tgt.id,
                "workspace_id": test_workspace.id
            })
        assert "合并" in result

    def test_merge_same_folder(self, session, test_workspace, test_folder):
        from app.features.folder.organize.tools import merge_folders
        with patch("app.features.folder.organize.tools.get_session", return_value=session):
            result = merge_folders.invoke({
                "source_folder_id": test_folder.id, "target_folder_id": test_folder.id,
                "workspace_id": test_workspace.id
            })
        assert "同一个" in result

    def test_merge_not_found(self, session, test_workspace):
        from app.features.folder.organize.tools import merge_folders
        with patch("app.features.folder.organize.tools.get_session", return_value=session):
            result = merge_folders.invoke({
                "source_folder_id": 99999, "target_folder_id": 99998,
                "workspace_id": test_workspace.id
            })
        assert "不存在" in result


class TestFindDuplicateFolders:
    def test_no_duplicates(self, session, test_workspace):
        from app.features.folder.organize.tools import find_duplicate_folders
        with patch("app.features.folder.organize.tools.get_session", return_value=session):
            result = find_duplicate_folders.invoke({"workspace_id": test_workspace.id})
        assert "未发现重复" in result

    def test_has_duplicates(self, session, test_workspace):
        from app.features.folder.organize.tools import find_duplicate_folders
        from app.models.folder import Folder
        f1 = Folder(name="same", workspace_id=test_workspace.id, parent_id=None)
        f2 = Folder(name="same", workspace_id=test_workspace.id, parent_id=None)
        session.add_all([f1, f2])
        session.flush()
        with patch("app.features.folder.organize.tools.get_session", return_value=session):
            result = find_duplicate_folders.invoke({"workspace_id": test_workspace.id})
        assert "重复" in result


class TestMoveFolder:
    def test_move_to_root(self, session, test_workspace, test_folder):
        from app.features.folder.organize.tools import move_folder
        with patch("app.features.folder.organize.tools.get_session", return_value=session), \
             patch("app.features.folder.organize.tools.clear_workspace_cache"):
            result = move_folder.invoke({
                "folder_id": test_folder.id, "target_folder_id": 0,
                "workspace_id": test_workspace.id
            })
        assert "根目录" in result

    def test_move_to_self(self, session, test_workspace, test_folder):
        from app.features.folder.organize.tools import move_folder
        with patch("app.features.folder.organize.tools.get_session", return_value=session):
            result = move_folder.invoke({
                "folder_id": test_folder.id, "target_folder_id": test_folder.id,
                "workspace_id": test_workspace.id
            })
        assert "自身" in result

    def test_move_not_found(self, session, test_workspace):
        from app.features.folder.organize.tools import move_folder
        with patch("app.features.folder.organize.tools.get_session", return_value=session):
            result = move_folder.invoke({
                "folder_id": 99999, "target_folder_id": 0,
                "workspace_id": test_workspace.id
            })
        assert "不存在" in result

    def test_move_wrong_workspace(self, session, test_workspace, test_folder):
        from app.features.folder.organize.tools import move_folder
        with patch("app.features.folder.organize.tools.get_session", return_value=session):
            result = move_folder.invoke({
                "folder_id": test_folder.id, "target_folder_id": 0,
                "workspace_id": 99999
            })
        assert "权限错误" in result


class TestAgentWrappers:
    def test_find_mixed_content_folders(self, session, test_workspace):
        from app.features.folder.organize.tools import find_mixed_content_folders
        with patch("app.features.folder.organize.tools.get_session", return_value=session):
            result = find_mixed_content_folders.invoke({"workspace_id": test_workspace.id})
        assert isinstance(result, str)

    def test_find_empty_folders(self, session, test_workspace, test_folder):
        from app.features.folder.organize.tools import find_empty_folders
        with patch("app.features.folder.organize.tools.get_session", return_value=session):
            result = find_empty_folders.invoke({"workspace_id": test_workspace.id})
        assert isinstance(result, str)
