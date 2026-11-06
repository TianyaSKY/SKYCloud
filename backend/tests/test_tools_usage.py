"""整理工具 @tool 函数测试 + token_usage service + 路由器补充。"""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.features.folder.organize.tools import (
    _merge_folder_contents,  # pyright: ignore[reportPrivateUsage]
    create_folder,
    delete_folder,
    find_duplicate_folders,
    find_empty_folders,
    find_mixed_content_folders,
    get_all_files,
    get_file_information,
    get_folder_tree,
    move_file,
    move_folder,
    rename_folder,
)
from app.features.token_usage.service import (
    GetUsageLogsParams,
    RecordUsageParams,
    get_daily_stats,
    get_usage_logs,
    get_user_token_stats,
    record_usage,
)
from app.models.folder import Folder
from app.models.token_usage_log import TokenUsageLog
from app.models.user import User


class TestGetAllFilesTool:
    def test_no_files(self) -> None:
        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.all.return_value = []
        with patch(
            "app.features.folder.organize.tools.get_session",
            return_value=mock_session,
        ):
            result = get_all_files.invoke({"workspace_id": 1})
        assert "没有找到文件" in str(result)
        mock_session.close.assert_called_once()

    def test_with_files(self) -> None:
        mock_file = MagicMock()
        mock_file.id = 1
        mock_file.name = "test.txt"
        mock_file.parent_id = None

        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.all.return_value = [
            mock_file
        ]
        with patch(
            "app.features.folder.organize.tools.get_session",
            return_value=mock_session,
        ):
            result = get_all_files.invoke({"workspace_id": 1})
        assert "test.txt" in str(result)


class TestGetFolderTreeTool:
    def test_with_folders(self) -> None:
        mock_folder = MagicMock()
        mock_folder.id = 1
        mock_folder.name = "docs"
        mock_folder.parent_id = None

        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.all.return_value = [
            mock_folder
        ]
        with patch(
            "app.features.folder.organize.tools.get_session",
            return_value=mock_session,
        ):
            result = get_folder_tree.invoke({"workspace_id": 1})
        assert "docs" in str(result)


class TestCreateFolderTool:
    def test_existing_folder(self) -> None:
        mock_existing = MagicMock()
        mock_existing.id = 5

        mock_session = MagicMock()
        mock_session.get.return_value.workspace_id = 1
        mock_session.get.return_value.id = 1
        mock_session.get.return_value.parent_id = None
        mock_session.query.return_value.filter_by.return_value.first.return_value = (
            mock_existing
        )
        with patch(
            "app.features.folder.organize.tools.get_session",
            return_value=mock_session,
        ):
            result = create_folder.invoke(
                {"name": "docs", "parent_id": 1, "workspace_id": 1}
            )
        assert "已存在" in str(result)

    def test_create_new(self) -> None:
        mock_session = MagicMock()
        mock_session.get.return_value.workspace_id = 1
        mock_session.get.return_value.id = 1
        mock_session.get.return_value.parent_id = None
        mock_session.query.return_value.filter_by.return_value.first.return_value = (
            None
        )
        mock_new_folder = MagicMock()
        mock_new_folder.id = 10

        with (
            patch(
                "app.features.folder.organize.tools.get_session",
                return_value=mock_session,
            ),
            patch("app.features.folder.organize.tools.clear_workspace_cache"),
        ):

            def fake_refresh(obj: Any) -> None:
                obj.id = 10

            mock_session.refresh.side_effect = fake_refresh
            result = create_folder.invoke(
                {"name": "new_folder", "parent_id": 1, "workspace_id": 1}
            )
        assert "已创建" in str(result) or "ID" in str(result)


class TestRenameFolderTool:
    def test_folder_not_found(self) -> None:
        mock_session = MagicMock()
        mock_session.get.return_value = None
        with patch(
            "app.features.folder.organize.tools.get_session",
            return_value=mock_session,
        ):
            result = rename_folder.invoke(
                {"folder_id": 999, "new_name": "x", "workspace_id": 1}
            )
        assert "不存在" in str(result)

    def test_wrong_workspace(self) -> None:
        mock_folder = MagicMock()
        mock_folder.workspace_id = 999

        mock_session = MagicMock()
        mock_session.get.return_value = mock_folder
        with patch(
            "app.features.folder.organize.tools.get_session",
            return_value=mock_session,
        ):
            result = rename_folder.invoke(
                {"folder_id": 1, "new_name": "x", "workspace_id": 1}
            )
        assert "权限错误" in str(result)

    def test_success(self) -> None:
        mock_folder = MagicMock()
        mock_folder.id = 1
        mock_folder.workspace_id = 1
        mock_folder.parent_id = None

        mock_session = MagicMock()
        mock_session.get.return_value = mock_folder
        mock_session.query.return_value.filter_by.return_value.first.return_value = (
            None
        )

        with (
            patch(
                "app.features.folder.organize.tools.get_session",
                return_value=mock_session,
            ),
            patch("app.features.folder.organize.tools.clear_workspace_cache"),
        ):
            result = rename_folder.invoke(
                {"folder_id": 1, "new_name": "renamed", "workspace_id": 1}
            )
        assert "重命名" in str(result)


class TestMoveFileTool:
    def test_file_not_found(self) -> None:
        mock_session = MagicMock()
        mock_session.get.return_value = None
        with patch(
            "app.features.folder.organize.tools.get_session",
            return_value=mock_session,
        ):
            result = move_file.invoke(
                {"file_id": 999, "target_folder_id": 1, "workspace_id": 1}
            )
        assert "不存在" in str(result)

    def test_success(self) -> None:
        mock_file = MagicMock()
        mock_file.id = 1
        mock_file.workspace_id = 1

        mock_folder = MagicMock()
        mock_folder.workspace_id = 1

        mock_session = MagicMock()
        mock_session.get.side_effect = [mock_file, mock_folder]

        with (
            patch(
                "app.features.folder.organize.tools.get_session",
                return_value=mock_session,
            ),
            patch("app.features.folder.organize.tools.clear_workspace_cache"),
        ):
            result = move_file.invoke(
                {"file_id": 1, "target_folder_id": 2, "workspace_id": 1}
            )
        assert "移动" in str(result)


class TestGetFileInformationTool:
    def test_file_not_found(self) -> None:
        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.first.return_value = (
            None
        )
        with patch(
            "app.features.folder.organize.tools.get_session",
            return_value=mock_session,
        ):
            result = get_file_information.invoke({"file_id": 999})
        assert "不存在" in str(result)

    def test_success(self) -> None:
        mock_file = MagicMock()
        mock_file.id = 1
        mock_file.name = "test.txt"
        mock_file.parent_id = None
        mock_file.file_size = 100
        mock_file.mime_type = "text/plain"
        mock_file.description = "A test file"

        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.first.return_value = (
            mock_file
        )
        with patch(
            "app.features.folder.organize.tools.get_session",
            return_value=mock_session,
        ):
            result = get_file_information.invoke({"file_id": 1})
        assert "test.txt" in str(result)


class TestDeleteFolderTool:
    def test_folder_not_found(self) -> None:
        mock_session = MagicMock()
        mock_session.get.return_value = None
        with patch(
            "app.features.folder.organize.tools.get_session",
            return_value=mock_session,
        ):
            result = delete_folder.invoke({"folder_id": 999, "workspace_id": 1})
        assert "不存在" in str(result)

    def test_not_empty_subfolders(self) -> None:
        mock_folder = MagicMock()
        mock_folder.id = 1
        mock_folder.workspace_id = 1

        mock_session = MagicMock()
        mock_session.get.return_value = mock_folder
        mock_session.query.return_value.filter_by.return_value.count.return_value = (
            1
        )

        with patch(
            "app.features.folder.organize.tools.get_session",
            return_value=mock_session,
        ):
            result = delete_folder.invoke({"folder_id": 1, "workspace_id": 1})
        assert "不为空" in str(result)

    def test_success(self) -> None:
        mock_folder = MagicMock()
        mock_folder.id = 1
        mock_folder.workspace_id = 1

        mock_session = MagicMock()
        mock_session.get.return_value = mock_folder
        mock_session.query.return_value.filter_by.return_value.count.return_value = (
            0
        )

        with (
            patch(
                "app.features.folder.organize.tools.get_session",
                return_value=mock_session,
            ),
            patch("app.features.folder.organize.tools.clear_workspace_cache"),
        ):
            result = delete_folder.invoke({"folder_id": 1, "workspace_id": 1})
        assert "已删除" in str(result)


class TestFindDuplicateFoldersTool:
    def test_no_duplicates(self) -> None:
        mock_folder = MagicMock()
        mock_folder.id = 1
        mock_folder.name = "docs"
        mock_folder.parent_id = None

        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.all.return_value = [
            mock_folder
        ]
        with patch(
            "app.features.folder.organize.tools.get_session",
            return_value=mock_session,
        ):
            result = find_duplicate_folders.invoke({"workspace_id": 1})
        assert "未发现重复" in str(result)

    def test_with_duplicates(self) -> None:
        f1 = MagicMock(id=1, name="docs", parent_id=None)
        f2 = MagicMock(id=2, name="docs", parent_id=None)

        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.all.return_value = [
            f1,
            f2,
        ]
        with patch(
            "app.features.folder.organize.tools.get_session",
            return_value=mock_session,
        ):
            result = find_duplicate_folders.invoke({"workspace_id": 1})
        assert "重复" in str(result)


class TestMoveFolderTool:
    def test_folder_not_found(self) -> None:
        mock_session = MagicMock()
        mock_session.get.return_value = None
        with patch(
            "app.features.folder.organize.tools.get_session",
            return_value=mock_session,
        ):
            result = move_folder.invoke(
                {"folder_id": 999, "target_folder_id": 1, "workspace_id": 1}
            )
        assert "不存在" in str(result)

    def test_move_to_root(self) -> None:
        mock_folder = MagicMock()
        mock_folder.id = 1
        mock_folder.workspace_id = 1
        mock_folder.parent_id = 2

        mock_session = MagicMock()
        root = Folder(id=2, workspace_id=1, name="/", parent_id=None)
        mock_session.scalar.return_value = 2
        mock_session.get.side_effect = [mock_folder, root]

        with (
            patch(
                "app.features.folder.organize.tools.get_session",
                return_value=mock_session,
            ),
            patch("app.features.folder.organize.tools.clear_workspace_cache"),
        ):
            result = move_folder.invoke(
                {"folder_id": 1, "target_folder_id": 0, "workspace_id": 1}
            )
        assert "根目录" in str(result)


class TestFindMixedContentFoldersTool:
    def test_delegates(self) -> None:
        with patch(
            "app.features.folder.organize.tools.check_mixed_folders_internal",
            return_value=(True, "OK"),
        ):
            result = find_mixed_content_folders.invoke({"workspace_id": 1})
        assert result == "OK"


class TestFindEmptyFoldersTool:
    def test_delegates(self) -> None:
        with patch(
            "app.features.folder.organize.tools.check_empty_folders_internal",
            return_value=(True, "OK"),
        ):
            result = find_empty_folders.invoke({"workspace_id": 1})
        assert result == "OK"


class TestMergeFolderContents:
    def test_basic_merge(self) -> None:
        mock_session = MagicMock()
        source = MagicMock(id=1)
        target = MagicMock(id=2)

        mock_file = MagicMock()
        mock_file.parent_id = 1

        # 第一次查询返回文件，第二次返回空子文件夹列表
        mock_session.query.return_value.filter_by.return_value.all.side_effect = [
            [mock_file],
            [],
        ]

        _merge_folder_contents(mock_session, source, target)
        assert mock_file.parent_id == 2


class TestRecordUsage:
    def test_success(self) -> None:
        mock_session = MagicMock()
        with patch(
            "app.features.token_usage.service.SessionLocal",
            return_value=mock_session,
        ):
            record_usage(
                RecordUsageParams(
                    user_id=1,
                    action="chat",
                    model_name="test-model",
                    prompt_tokens=10,
                    completion_tokens=20,
                    total_tokens=30,
                    query_summary="test query",
                )
            )
        mock_session.commit.assert_called_once()
        mock_session.close.assert_called_once()

    def test_zero_total_calculates(self) -> None:
        mock_session = MagicMock()
        with patch(
            "app.features.token_usage.service.SessionLocal",
            return_value=mock_session,
        ):
            record_usage(
                RecordUsageParams(
                    user_id=1,
                    action="chat",
                    prompt_tokens=10,
                    completion_tokens=20,
                )
            )
        mock_session.commit.assert_called_once()


class TestGetUserTokenStats:
    def test_user_not_found(self, session: Session) -> None:
        from app.exceptions import ResourceNotFoundError

        with pytest.raises(ResourceNotFoundError):
            get_user_token_stats(session, 99999)

    def test_success(self, session: Session, test_user: User) -> None:
        result = get_user_token_stats(session, int(test_user.id))
        assert "user_id" in result
        assert result["user_id"] == test_user.id


class TestGetUsageLogs:
    def test_basic(self, session: Session, test_user: User) -> None:
        session.add(
            TokenUsageLog(
                user_id=int(test_user.id),
                action="chat",
                model_name="m",
                prompt_tokens=10,
                completion_tokens=20,
                total_tokens=30,
            )
        )
        session.flush()
        result = get_usage_logs(
            session, GetUsageLogsParams(user_id=int(test_user.id))
        )
        assert result["total"] >= 1

    def test_with_action_filter(self, session: Session, test_user: User) -> None:
        session.add(
            TokenUsageLog(
                user_id=int(test_user.id),
                action="chat",
                model_name="m",
                prompt_tokens=10,
                completion_tokens=20,
                total_tokens=30,
            )
        )
        session.flush()
        result = get_usage_logs(
            session,
            GetUsageLogsParams(user_id=int(test_user.id), action="chat"),
        )
        assert result["total"] >= 1


class TestGetDailyStats:
    def test_basic(self, session: Session, test_user: User) -> None:
        session.add(
            TokenUsageLog(
                user_id=int(test_user.id),
                action="chat",
                model_name="m",
                prompt_tokens=10,
                completion_tokens=20,
                total_tokens=30,
            )
        )
        session.flush()
        result = get_daily_stats(session, int(test_user.id), days=7)
        assert isinstance(result, list)
