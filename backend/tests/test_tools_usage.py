"""整理工具 @tool 函数测试 + token_usage service + 路由器补充。"""

import pytest
from unittest.mock import patch, MagicMock

from app.models.file import File
from app.models.folder import Folder


# ---------------------------------------------------------------------------
# organize tools（通过 .invoke 调用 @tool 包装的函数）
# ---------------------------------------------------------------------------

from app.features.folder.organize.tools import (
    get_all_files,
    get_folder_tree,
    create_folder,
    rename_folder,
    move_file,
    get_file_information,
    delete_folder,
    merge_folders,
    find_duplicate_folders,
    move_folder,
    find_mixed_content_folders,
    find_empty_folders,
    _merge_folder_contents,
)


class TestGetAllFilesTool:
    def test_no_files(self):
        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.all.return_value = []
        with patch("app.features.folder.organize.tools.get_session", return_value=mock_session):
            result = get_all_files.invoke({"workspace_id": 1})
        assert "没有找到文件" in result
        mock_session.close.assert_called_once()

    def test_with_files(self):
        mock_file = MagicMock()
        mock_file.id = 1
        mock_file.name = "test.txt"
        mock_file.parent_id = None

        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.all.return_value = [mock_file]
        with patch("app.features.folder.organize.tools.get_session", return_value=mock_session):
            result = get_all_files.invoke({"workspace_id": 1})
        assert "test.txt" in result


class TestGetFolderTreeTool:
    def test_with_folders(self):
        mock_folder = MagicMock()
        mock_folder.id = 1
        mock_folder.name = "docs"
        mock_folder.parent_id = None

        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.all.return_value = [mock_folder]
        with patch("app.features.folder.organize.tools.get_session", return_value=mock_session):
            result = get_folder_tree.invoke({"workspace_id": 1})
        assert "docs" in result


class TestCreateFolderTool:
    def test_existing_folder(self):
        mock_existing = MagicMock()
        mock_existing.id = 5

        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = mock_existing
        with patch("app.features.folder.organize.tools.get_session", return_value=mock_session):
            result = create_folder.invoke({"name": "docs", "parent_id": 1, "workspace_id": 1})
        assert "已存在" in result

    def test_create_new(self):
        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = None
        mock_new_folder = MagicMock()
        mock_new_folder.id = 10

        with patch("app.features.folder.organize.tools.get_session", return_value=mock_session), \
             patch("app.features.folder.organize.tools.clear_workspace_cache"):
            # 模拟 session.add 后 refresh 设置 id
            def fake_refresh(obj):
                obj.id = 10
            mock_session.refresh.side_effect = fake_refresh
            result = create_folder.invoke({"name": "new_folder", "parent_id": 1, "workspace_id": 1})
        assert "已创建" in result or "ID" in result


class TestRenameFolderTool:
    def test_folder_not_found(self):
        mock_session = MagicMock()
        mock_session.get.return_value = None
        with patch("app.features.folder.organize.tools.get_session", return_value=mock_session):
            result = rename_folder.invoke({"folder_id": 999, "new_name": "x", "workspace_id": 1})
        assert "不存在" in result

    def test_wrong_workspace(self):
        mock_folder = MagicMock()
        mock_folder.workspace_id = 999

        mock_session = MagicMock()
        mock_session.get.return_value = mock_folder
        with patch("app.features.folder.organize.tools.get_session", return_value=mock_session):
            result = rename_folder.invoke({"folder_id": 1, "new_name": "x", "workspace_id": 1})
        assert "权限错误" in result

    def test_success(self):
        mock_folder = MagicMock()
        mock_folder.id = 1
        mock_folder.workspace_id = 1
        mock_folder.parent_id = None

        mock_session = MagicMock()
        mock_session.get.return_value = mock_folder
        mock_session.query.return_value.filter_by.return_value.first.return_value = None

        with patch("app.features.folder.organize.tools.get_session", return_value=mock_session), \
             patch("app.features.folder.organize.tools.clear_workspace_cache"):
            result = rename_folder.invoke({"folder_id": 1, "new_name": "renamed", "workspace_id": 1})
        assert "重命名" in result


class TestMoveFileTool:
    def test_file_not_found(self):
        mock_session = MagicMock()
        mock_session.get.return_value = None
        with patch("app.features.folder.organize.tools.get_session", return_value=mock_session):
            result = move_file.invoke({"file_id": 999, "target_folder_id": 1, "workspace_id": 1})
        assert "不存在" in result

    def test_success(self):
        mock_file = MagicMock()
        mock_file.id = 1
        mock_file.workspace_id = 1

        mock_folder = MagicMock()
        mock_folder.workspace_id = 1

        mock_session = MagicMock()
        mock_session.get.side_effect = [mock_file, mock_folder]

        with patch("app.features.folder.organize.tools.get_session", return_value=mock_session), \
             patch("app.features.folder.organize.tools.clear_workspace_cache"):
            result = move_file.invoke({"file_id": 1, "target_folder_id": 2, "workspace_id": 1})
        assert "移动" in result


class TestGetFileInformationTool:
    def test_file_not_found(self):
        mock_session = MagicMock()
        mock_session.get.return_value = None
        with patch("app.features.folder.organize.tools.get_session", return_value=mock_session):
            result = get_file_information.invoke({"file_id": 999})
        assert "不存在" in result

    def test_success(self):
        mock_file = MagicMock()
        mock_file.id = 1
        mock_file.name = "test.txt"
        mock_file.parent_id = None
        mock_file.file_size = 100
        mock_file.mime_type = "text/plain"
        mock_file.description = "A test file"

        mock_session = MagicMock()
        mock_session.get.return_value = mock_file
        with patch("app.features.folder.organize.tools.get_session", return_value=mock_session):
            result = get_file_information.invoke({"file_id": 1})
        assert "test.txt" in result


class TestDeleteFolderTool:
    def test_folder_not_found(self):
        mock_session = MagicMock()
        mock_session.get.return_value = None
        with patch("app.features.folder.organize.tools.get_session", return_value=mock_session):
            result = delete_folder.invoke({"folder_id": 999, "workspace_id": 1})
        assert "不存在" in result

    def test_not_empty_subfolders(self):
        mock_folder = MagicMock()
        mock_folder.id = 1
        mock_folder.workspace_id = 1

        mock_session = MagicMock()
        mock_session.get.return_value = mock_folder
        mock_session.query.return_value.filter_by.return_value.count.return_value = 1

        with patch("app.features.folder.organize.tools.get_session", return_value=mock_session):
            result = delete_folder.invoke({"folder_id": 1, "workspace_id": 1})
        assert "不为空" in result

    def test_success(self):
        mock_folder = MagicMock()
        mock_folder.id = 1
        mock_folder.workspace_id = 1

        mock_session = MagicMock()
        mock_session.get.return_value = mock_folder
        mock_session.query.return_value.filter_by.return_value.count.return_value = 0

        with patch("app.features.folder.organize.tools.get_session", return_value=mock_session), \
             patch("app.features.folder.organize.tools.clear_workspace_cache"):
            result = delete_folder.invoke({"folder_id": 1, "workspace_id": 1})
        assert "已删除" in result


class TestFindDuplicateFoldersTool:
    def test_no_duplicates(self):
        mock_folder = MagicMock()
        mock_folder.id = 1
        mock_folder.name = "docs"
        mock_folder.parent_id = None

        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.all.return_value = [mock_folder]
        with patch("app.features.folder.organize.tools.get_session", return_value=mock_session):
            result = find_duplicate_folders.invoke({"workspace_id": 1})
        assert "未发现重复" in result

    def test_with_duplicates(self):
        f1 = MagicMock(id=1, name="docs", parent_id=None)
        f2 = MagicMock(id=2, name="docs", parent_id=None)

        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.all.return_value = [f1, f2]
        with patch("app.features.folder.organize.tools.get_session", return_value=mock_session):
            result = find_duplicate_folders.invoke({"workspace_id": 1})
        assert "重复" in result


class TestMoveFolderTool:
    def test_folder_not_found(self):
        mock_session = MagicMock()
        mock_session.get.return_value = None
        with patch("app.features.folder.organize.tools.get_session", return_value=mock_session):
            result = move_folder.invoke({"folder_id": 999, "target_folder_id": 1, "workspace_id": 1})
        assert "不存在" in result

    def test_move_to_root(self):
        mock_folder = MagicMock()
        mock_folder.id = 1
        mock_folder.workspace_id = 1

        mock_session = MagicMock()
        mock_session.get.return_value = mock_folder

        with patch("app.features.folder.organize.tools.get_session", return_value=mock_session), \
             patch("app.features.folder.organize.tools.clear_workspace_cache"):
            result = move_folder.invoke({"folder_id": 1, "target_folder_id": 0, "workspace_id": 1})
        assert "根目录" in result


class TestFindMixedContentFoldersTool:
    def test_delegates(self):
        with patch("app.features.folder.organize.tools.check_mixed_folders_internal", return_value=(True, "OK")):
            result = find_mixed_content_folders.invoke({"workspace_id": 1})
        assert result == "OK"


class TestFindEmptyFoldersTool:
    def test_delegates(self):
        with patch("app.features.folder.organize.tools.check_empty_folders_internal", return_value=(True, "OK")):
            result = find_empty_folders.invoke({"workspace_id": 1})
        assert result == "OK"


class TestMergeFolderContents:
    def test_basic_merge(self):
        mock_session = MagicMock()
        source = MagicMock(id=1)
        target = MagicMock(id=2)

        mock_file = MagicMock()
        mock_file.parent_id = 1

        # 第一次查询返回文件，第二次返回空子文件夹列表
        mock_session.query.return_value.filter_by.return_value.all.side_effect = [[mock_file], []]

        _merge_folder_contents(mock_session, source, target)
        assert mock_file.parent_id == 2


# ---------------------------------------------------------------------------
# token_usage service 补充
# ---------------------------------------------------------------------------

from app.features.token_usage.service import (
    record_usage,
    get_user_token_stats,
    get_usage_logs,
    get_daily_stats,
)
from app.models.token_usage_log import TokenUsageLog


class TestRecordUsage:
    def test_success(self):
        mock_session = MagicMock()
        with patch("app.features.token_usage.service.SessionLocal", return_value=mock_session):
            record_usage(
                user_id=1,
                action="chat",
                model_name="test-model",
                prompt_tokens=10,
                completion_tokens=20,
                total_tokens=30,
                query_summary="test query",
            )
        mock_session.commit.assert_called_once()
        mock_session.close.assert_called_once()

    def test_zero_total_calculates(self):
        mock_session = MagicMock()
        with patch("app.features.token_usage.service.SessionLocal", return_value=mock_session):
            record_usage(user_id=1, action="chat", prompt_tokens=10, completion_tokens=20)
        mock_session.commit.assert_called_once()


class TestGetUserTokenStats:
    def test_user_not_found(self, session):
        result = get_user_token_stats(session, 99999)
        assert result == {}

    def test_success(self, session, test_user):
        result = get_user_token_stats(session, test_user.id)
        assert "user_id" in result
        assert result["user_id"] == test_user.id


class TestGetUsageLogs:
    def test_basic(self, session, test_user):
        session.add(TokenUsageLog(
            user_id=test_user.id, action="chat", model_name="m",
            prompt_tokens=10, completion_tokens=20, total_tokens=30
        ))
        session.flush()
        result = get_usage_logs(session, test_user.id)
        assert result["total"] >= 1

    def test_with_action_filter(self, session, test_user):
        session.add(TokenUsageLog(
            user_id=test_user.id, action="chat", model_name="m",
            prompt_tokens=10, completion_tokens=20, total_tokens=30
        ))
        session.flush()
        result = get_usage_logs(session, test_user.id, action="chat")
        assert result["total"] >= 1


class TestGetDailyStats:
    def test_basic(self, session, test_user):
        session.add(TokenUsageLog(
            user_id=test_user.id, action="chat", model_name="m",
            prompt_tokens=10, completion_tokens=20, total_tokens=30
        ))
        session.flush()
        result = get_daily_stats(session, test_user.id, days=7)
        assert isinstance(result, list)
