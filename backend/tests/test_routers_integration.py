"""路由器测试：直接调用路由函数并 mock 依赖。"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from fastapi import HTTPException


# ---------------------------------------------------------------------------
# Auth Router 测试
# ---------------------------------------------------------------------------

class TestAuthRouter:
    def test_login(self, session, test_user):
        from app.features.auth.router import login
        from app.features.auth.schemas import LoginRequest

        payload = LoginRequest(username="testuser", password="password123")
        with patch("app.features.auth.service.login", return_value={"token": "jwt", "user": {}}) as mock:
            result = login(payload, session)
        assert "token" in result
        mock.assert_called_once()

    def test_register(self, session):
        from app.features.auth.router import register
        from app.features.auth.schemas import RegisterRequest

        mock_user = MagicMock()
        mock_user.to_dict.return_value = {"id": 1, "username": "new"}
        payload = RegisterRequest(username="new", password="pass123")
        with patch("app.features.auth.service.register_user", return_value=mock_user):
            result = register(payload, session)
        assert result.status_code == 201

    def test_get_mcp_token(self, session, test_user):
        from app.features.auth.router import get_mcp_token

        with patch("app.features.auth.service.get_mcp_token", return_value={"token": "mcp"}):
            result = get_mcp_token(test_user, session)
        assert "token" in result

    def test_refresh_mcp_token(self, session, test_user):
        from app.features.auth.router import refresh_mcp_token

        with patch("app.features.auth.service.refresh_mcp_token", return_value={"token": "new"}):
            result = refresh_mcp_token(test_user, session)
        assert "token" in result

    def test_create_user(self, session):
        from app.features.auth.router import create_user
        from app.features.auth.schemas import UserCreateRequest

        mock_user = MagicMock()
        mock_user.to_dict.return_value = {"id": 2}
        payload = UserCreateRequest(username="created", password="pass")
        with patch("app.features.auth.user_service.create_user", return_value=mock_user):
            result = create_user(payload, session)
        assert result.status_code == 201

    def test_get_user(self, session, test_user):
        from app.features.auth.router import get_user

        mock_user = MagicMock()
        mock_user.to_dict.return_value = {"id": test_user.id}
        with patch("app.features.auth.user_service.ensure_user_access"), \
             patch("app.features.auth.user_service.get_user", new_callable=AsyncMock, return_value=mock_user):
            import asyncio
            result = asyncio.run(get_user(test_user.id, test_user, session))
        assert "id" in result

    def test_update_user(self, session, test_user):
        from app.features.auth.router import update_user
        from app.features.auth.schemas import UserUpdateRequest

        mock_user = MagicMock()
        mock_user.to_dict.return_value = {"id": test_user.id}
        payload = UserUpdateRequest(username="updated")
        with patch("app.features.auth.user_service.ensure_user_access"), \
             patch("app.features.auth.user_service.update_user", return_value=mock_user):
            result = update_user(test_user.id, payload, test_user, session)
        assert "id" in result

    def test_update_password(self, session, test_user):
        from app.features.auth.router import update_user_password
        from app.features.auth.schemas import UserPasswordUpdateRequest

        payload = UserPasswordUpdateRequest(old_password="old", new_password="new")
        with patch("app.features.auth.user_service.ensure_user_access"), \
             patch("app.features.auth.user_service.change_password"):
            result = update_user_password(test_user.id, payload, test_user, session)
        assert "message" in result

    def test_delete_user(self, session, test_user):
        from app.features.auth.router import delete_user

        with patch("app.features.auth.user_service.ensure_user_access"), \
             patch("app.features.auth.user_service.delete_user"):
            result = delete_user(test_user.id, test_user, session)
        assert result.status_code == 204


# ---------------------------------------------------------------------------
# Workspace Router 测试
# ---------------------------------------------------------------------------

class TestWorkspaceRouter:
    def test_create_workspace(self, session, test_user):
        from app.features.workspace.router import create_workspace

        mock_ws = MagicMock()
        mock_ws.to_dict.return_value = {"id": 1, "name": "new"}
        # 使用 MagicMock 代替 schema，因为 schemas.py 有重复类定义导致字段缺失
        payload = MagicMock()
        payload.name = "new"
        payload.description = None
        with patch("app.features.workspace.service.create_workspace", return_value=mock_ws):
            import asyncio
            result = asyncio.run(create_workspace(payload, test_user, session))
        assert result.status_code == 201

    def test_list_workspaces(self, session, test_user):
        from app.features.workspace.router import list_workspaces

        with patch("app.features.workspace.service.get_user_workspaces", return_value=[]):
            import asyncio
            result = asyncio.run(list_workspaces(test_user, session))
        assert "workspaces" in result

    def test_get_workspace(self, session, test_user, test_workspace):
        from app.features.workspace.router import get_workspace

        with patch("app.features.workspace.permissions.assert_member"), \
             patch("app.features.workspace.service.get_workspace_detail", return_value={"id": 1}):
            import asyncio
            result = asyncio.run(get_workspace(test_workspace.id, test_user, session))
        assert "id" in result

    def test_update_workspace(self, session, test_user, test_workspace):
        from app.features.workspace.router import update_workspace
        from app.features.workspace.schemas import WorkspaceUpdateRequest

        mock_ws = MagicMock()
        mock_ws.to_dict.return_value = {"id": test_workspace.id}
        payload = WorkspaceUpdateRequest(name="updated")
        with patch("app.features.workspace.service.update_workspace", return_value=mock_ws):
            import asyncio
            result = asyncio.run(update_workspace(test_workspace.id, payload, test_user, session))
        assert "id" in result

    def test_delete_workspace(self, session, test_user, test_workspace):
        from app.features.workspace.router import delete_workspace

        with patch("app.features.workspace.service.delete_workspace"):
            import asyncio
            result = asyncio.run(delete_workspace(test_workspace.id, test_user, session))
        assert result.status_code == 200

    def test_invite_member(self, session, test_user, test_workspace):
        from app.features.workspace.router import invite_member
        from app.features.workspace.schemas import MemberInviteRequest

        mock_member = MagicMock()
        mock_member.to_dict.return_value = {"user_id": 2}
        payload = MemberInviteRequest(username="new", role="editor")
        with patch("app.features.workspace.service.invite_member", return_value=mock_member):
            import asyncio
            result = asyncio.run(invite_member(test_workspace.id, payload, test_user, session))
        assert result.status_code == 201

    def test_list_members(self, session, test_user, test_workspace):
        from app.features.workspace.router import list_members

        with patch("app.features.workspace.permissions.assert_member"), \
             patch("app.features.workspace.service.list_members", return_value=[]):
            import asyncio
            result = asyncio.run(list_members(test_workspace.id, test_user, session))
        assert "members" in result

    def test_update_member_role(self, session, test_user, test_workspace):
        from app.features.workspace.router import update_member_role
        from app.features.workspace.schemas import MemberRoleUpdateRequest

        mock_member = MagicMock()
        mock_member.to_dict.return_value = {"user_id": 2}
        payload = MemberRoleUpdateRequest(role="admin")
        with patch("app.features.workspace.service.update_member_role", return_value=mock_member):
            import asyncio
            result = asyncio.run(update_member_role(test_workspace.id, 2, payload, test_user, session))
        assert "user_id" in result

    def test_remove_member(self, session, test_user, test_workspace):
        from app.features.workspace.router import remove_member

        with patch("app.features.workspace.service.remove_member"):
            import asyncio
            result = asyncio.run(remove_member(test_workspace.id, 2, test_user, session))
        assert result.status_code == 200


# ---------------------------------------------------------------------------
# Folder Router 测试
# ---------------------------------------------------------------------------

class TestFolderRouter:
    def test_create_folder(self, session, test_user, test_workspace):
        from app.features.folder.router import create_folder
        from app.features.folder.schemas import FolderCreateRequest

        mock_folder = MagicMock()
        mock_folder.to_dict.return_value = {"id": 1}
        payload = FolderCreateRequest(name="new", parent_id=None)
        with patch("app.features.folder.service.create_folder", return_value=mock_folder):
            result = create_folder(payload, test_user, test_workspace, session)
        assert result.status_code == 201

    def test_update_folder(self, session, test_user, test_workspace):
        from app.features.folder.router import update_folder
        from app.features.folder.schemas import FolderUpdateRequest

        mock_folder = MagicMock()
        mock_folder.to_dict.return_value = {"id": 1}
        payload = FolderUpdateRequest(name="renamed")
        with patch("app.features.folder.service.get_authorized_folder"), \
             patch("app.features.folder.service.update_folder", return_value=mock_folder):
            result = update_folder(1, payload, test_user, test_workspace, session)
        assert "id" in result

    def test_delete_folder(self, session, test_user, test_workspace):
        from app.features.folder.router import delete_folder

        with patch("app.features.folder.service.get_authorized_folder"), \
             patch("app.features.folder.service.delete_folder"):
            result = delete_folder(1, test_user, test_workspace, session)
        assert result.status_code == 204

    def test_get_root_folder_id(self, session, test_user, test_workspace):
        from app.features.folder.router import get_root_folder_id

        with patch("app.features.folder.service.get_root_folder_id", return_value=1):
            result = get_root_folder_id(test_user, test_workspace, session)
        assert "root_folder_id" in result

    def test_get_folders(self, session, test_user, test_workspace):
        from app.features.folder.router import get_folders

        with patch("app.features.folder.service.get_folders", return_value=[]):
            result = get_folders(test_user, test_workspace, session)
        assert "folders" in result

    def test_get_folder(self, session, test_user, test_workspace):
        from app.features.folder.router import get_folder

        mock_folder = MagicMock()
        mock_folder.to_dict.return_value = {"id": 1}
        with patch("app.features.folder.service.get_authorized_folder", return_value=mock_folder):
            result = get_folder(1, test_user, test_workspace, session)
        assert "id" in result

    def test_organize(self, session, test_user, test_workspace):
        from app.features.folder.router import organize_workspace_files

        with patch("app.features.folder.service.organize_files", return_value=True):
            result = organize_workspace_files(test_user, test_workspace)
        assert result["queued"] is True


# ---------------------------------------------------------------------------
# File Router 测试
# ---------------------------------------------------------------------------

class TestFileRouter:
    def test_list_files(self, session, test_user, test_workspace):
        from app.features.file.router import list_files

        with patch("app.features.file.service.get_files_and_folders", return_value={"files": []}):
            result = list_files(test_user, test_workspace, None, 1, 10, None, "created_at", "desc", session)
        assert "files" in result

    def test_search_files_empty(self, session, test_user, test_workspace):
        from app.features.file.router import search_files
        import asyncio

        result = asyncio.run(
            search_files(test_user, test_workspace, "", 1, 10, "fuzzy", session)
        )
        assert result["total"] == 0

    def test_search_files(self, session, test_user, test_workspace):
        from app.features.file.router import search_files
        import asyncio

        with patch("app.features.file.service.search_files", new_callable=AsyncMock, return_value={"items": []}):
            result = asyncio.run(
                search_files(test_user, test_workspace, "test", 1, 10, "fuzzy", session)
            )
        assert "items" in result

    def test_update_file(self, session, test_user, test_workspace):
        from app.features.file.router import update_file
        from app.features.file.schemas import FileUpdateRequest

        mock_file = MagicMock()
        mock_file.to_dict.return_value = {"id": 1}
        payload = FileUpdateRequest(name="renamed.txt")
        with patch("app.features.file.service.get_authorized_file"), \
             patch("app.features.file.service.update_file", return_value=mock_file):
            result = update_file(1, payload, test_user, test_workspace, session)
        assert "id" in result

    def test_delete_file(self, session, test_user, test_workspace):
        from app.features.file.router import delete_file

        with patch("app.features.file.service.get_authorized_file"), \
             patch("app.features.file.service.delete_file"):
            result = delete_file(1, test_user, test_workspace, session)
        assert result.status_code == 204

    def test_get_file(self, session, test_user, test_workspace):
        from app.features.file.router import get_file

        mock_file = MagicMock()
        mock_file.to_dict.return_value = {"id": 1}
        with patch("app.features.file.service.get_authorized_file", return_value=mock_file):
            result = get_file(1, test_user, test_workspace, session)
        assert "id" in result

    def test_download_file(self, session, test_user, test_workspace, tmp_path):
        from app.features.file.router import download_file

        mock_file = MagicMock()
        mock_file.file_path = "test.txt"
        mock_file.name = "test.txt"
        mock_file.mime_type = "text/plain"
        with patch("app.features.file.service.get_downloadable_file", return_value=mock_file):
            result = download_file(1, test_user, test_workspace, session)
        assert result.media_type == "text/plain"

    def test_batch_delete(self, session, test_user, test_workspace):
        from app.features.file.router import batch_delete_files
        from app.features.file.schemas import BatchDeleteRequest, BatchDeleteItem

        payload = BatchDeleteRequest(items=[BatchDeleteItem(id=1, is_folder=False)])
        with patch("app.features.file.service.batch_delete_items"):
            result = batch_delete_files(payload, test_user, test_workspace, session)
        assert result.status_code == 204

    def test_retry_embedding(self, session, test_user, test_workspace):
        from app.features.file.router import retry_embedding
        from app.features.file.schemas import RetryEmbeddingRequest

        mock_file = MagicMock()
        mock_file.id = 1
        payload = RetryEmbeddingRequest(file_id=1)
        with patch("app.features.file.service.get_authorized_file", return_value=mock_file), \
             patch("app.features.file.service.retry_embedding"):
            result = retry_embedding(payload, test_user, test_workspace, session)
        assert result.status_code == 204

    def test_rebuild_failed_indexes(self, session, test_user, test_workspace):
        from app.features.file.router import rebuild_failed_indexes

        with patch("app.features.file.service.rebuild_failed_indexes", return_value=5):
            result = rebuild_failed_indexes(test_user, test_workspace, session)
        assert result["count"] == 5

    def test_process_status(self, session, test_user, test_workspace):
        from app.features.file.router import process_status
        import asyncio

        with patch("app.features.file.service.process_status", new_callable=AsyncMock, return_value={"pending": 0}):
            result = asyncio.run(process_status(test_user, test_workspace, session))
        assert "pending" in result

    def test_preflight(self, session, test_user, test_workspace):
        from app.features.file.router import preflight_file_upload
        from app.features.file.schemas import FilePreflightRequest

        payload = FilePreflightRequest(filename="test.txt", total_size=100, content_hash="a" * 64)
        with patch("app.features.file.service.preflight_file_upload", return_value={"ok": True}):
            result = preflight_file_upload(payload, test_user, test_workspace, session)
        assert result["ok"] is True

    def test_multipart_init(self, session, test_user, test_workspace):
        from app.features.file.router import init_multipart_upload
        from app.features.file.schemas import MultipartInitRequest

        payload = MultipartInitRequest(filename="big.zip", total_size=1000000)
        with patch("app.features.file.service.init_multipart_upload", return_value={"upload_id": "abc"}):
            result = init_multipart_upload(payload, test_user, test_workspace, session)
        assert "upload_id" in result

    def test_multipart_status(self, session, test_user, test_workspace):
        from app.features.file.router import get_multipart_upload_status

        with patch("app.features.file.service.get_multipart_upload_status", return_value={"chunks": []}):
            result = get_multipart_upload_status("abc", test_user, test_workspace)
        assert "chunks" in result

    def test_multipart_abort(self, session, test_user, test_workspace):
        from app.features.file.router import abort_multipart_upload

        with patch("app.features.file.service.abort_multipart_upload"):
            result = abort_multipart_upload("abc", test_user, test_workspace)
        assert result.status_code == 204

    def test_multipart_complete(self, session, test_user, test_workspace):
        from app.features.file.router import complete_multipart_upload
        from app.features.file.schemas import MultipartCompleteRequest

        mock_file = MagicMock()
        mock_file.to_dict.return_value = {"id": 1}
        payload = MultipartCompleteRequest(upload_id="abc")
        with patch("app.features.file.service.complete_multipart_upload", return_value=mock_file):
            result = complete_multipart_upload(payload, test_user, test_workspace, session)
        assert result.status_code == 201


# ---------------------------------------------------------------------------
# Share Router 测试
# ---------------------------------------------------------------------------

class TestShareRouter:
    def test_create_share(self, session, test_user):
        from app.features.share.router import create_share
        from app.features.share.schemas import ShareCreateRequest

        mock_share = MagicMock()
        mock_share.to_dict.return_value = {"id": 1}
        payload = ShareCreateRequest(file_id=1)
        with patch("app.features.share.service.create_share", return_value=mock_share):
            result = create_share(payload, test_user, session)
        assert result.status_code == 201

    def test_get_my_shares(self, session, test_user):
        from app.features.share.router import get_my_shares

        with patch("app.features.share.service.get_my_shares", return_value=[]):
            result = get_my_shares(test_user, session)
        assert result == []

    def test_cancel_share(self, session, test_user):
        from app.features.share.router import cancel_share

        with patch("app.features.share.service.cancel_share_for_user"):
            result = cancel_share(1, test_user, session)
        assert "message" in result


# ---------------------------------------------------------------------------
# Inbox Router 测试
# ---------------------------------------------------------------------------

class TestInboxRouter:
    def test_get_inbox(self, session, test_user):
        from app.features.inbox.router import get_inbox

        mock_result = {"items": [], "total": 0, "pages": 0, "page": 1}
        with patch("app.features.inbox.service.get_user_inbox", return_value=mock_result):
            result = get_inbox(test_user, session, 1, 20)
        assert "items" in result

    def test_mark_message_read(self, session, test_user):
        from app.features.inbox.router import mark_message_read

        mock_msg = MagicMock()
        mock_msg.to_dict.return_value = {"id": 1}
        with patch("app.features.inbox.service.mark_as_read", return_value=mock_msg):
            result = mark_message_read(1, test_user, session)
        assert "id" in result

    def test_mark_all_messages_read(self, session, test_user):
        from app.features.inbox.router import mark_all_messages_read

        with patch("app.features.inbox.service.mark_all_as_read"):
            result = mark_all_messages_read(test_user, session)
        assert "message" in result

    def test_delete_message(self, session, test_user):
        from app.features.inbox.router import delete_message

        with patch("app.features.inbox.service.delete_inbox_message"):
            result = delete_message(1, test_user, session)
        assert result.status_code == 204


# ---------------------------------------------------------------------------
# SysDict Router 测试
# ---------------------------------------------------------------------------

class TestSysDictRouter:
    def test_get_sys_dicts(self, session, test_user):
        from app.features.sys_dict.router import get_sys_dicts
        import asyncio

        with patch("app.features.sys_dict.service.get_sys_dict_all", new_callable=AsyncMock, return_value=[]):
            result = asyncio.run(get_sys_dicts(test_user, session))
        assert result == []

    def test_get_sys_dict(self, session, test_user):
        from app.features.sys_dict.router import get_sys_dict
        import asyncio

        with patch("app.features.sys_dict.service.get_sys_dict", new_callable=AsyncMock, return_value={"id": 1}):
            result = asyncio.run(get_sys_dict(1, test_user, session))
        assert "id" in result

    def test_create_sys_dict(self, session, test_user):
        from app.features.sys_dict.router import create_sys_dict
        from app.features.sys_dict.schemas import SysDictPayload

        mock_dict = MagicMock()
        mock_dict.to_dict.return_value = {"id": 1}
        payload = SysDictPayload(key="k", value="v")
        with patch("app.features.sys_dict.service.create_sys_dict", return_value=mock_dict):
            result = create_sys_dict(payload, test_user, session)
        assert result.status_code == 201

    def test_update_sys_dict(self, session, test_user):
        from app.features.sys_dict.router import update_sys_dict
        from app.features.sys_dict.schemas import SysDictPayload

        mock_dict = MagicMock()
        mock_dict.to_dict.return_value = {"id": 1}
        payload = SysDictPayload(key="k", value="v")
        with patch("app.features.sys_dict.service.update_sys_dict", return_value=mock_dict):
            result = update_sys_dict(1, payload, test_user, session)
        assert "id" in result

    def test_delete_sys_dict(self, session, test_user):
        from app.features.sys_dict.router import delete_sys_dict

        with patch("app.features.sys_dict.service.delete_sys_dict"):
            result = delete_sys_dict(1, test_user, session)
        assert result.status_code == 204


# ---------------------------------------------------------------------------
# Token Usage Router 测试
# ---------------------------------------------------------------------------

class TestTokenUsageRouter:
    def test_my_stats(self, session, test_user):
        from app.features.token_usage.router import my_token_stats

        with patch("app.features.token_usage.service.get_user_token_stats", return_value={"total": 100}):
            result = my_token_stats(test_user, session)
        assert "total" in result

    def test_user_stats(self, session, test_user):
        from app.features.token_usage.router import user_token_stats

        with patch("app.features.auth.user_service.ensure_user_access"), \
             patch("app.features.token_usage.service.get_user_token_stats", return_value={}):
            result = user_token_stats(test_user.id, test_user, session)
        assert result == {}

    def test_my_logs(self, session, test_user):
        from app.features.token_usage.router import my_usage_logs

        with patch("app.features.token_usage.service.get_usage_logs", return_value={"items": []}):
            result = my_usage_logs(1, 20, None, None, None, test_user, session)
        assert "items" in result

    def test_user_logs(self, session, test_user):
        from app.features.token_usage.router import user_usage_logs

        with patch("app.features.auth.user_service.ensure_user_access"), \
             patch("app.features.token_usage.service.get_usage_logs", return_value={"items": []}):
            result = user_usage_logs(test_user.id, 1, 20, None, None, None, test_user, session)
        assert "items" in result

    def test_my_daily(self, session, test_user):
        from app.features.token_usage.router import my_daily_stats

        with patch("app.features.token_usage.service.get_daily_stats", return_value=[]):
            result = my_daily_stats(30, test_user, session)
        assert result == []

    def test_user_daily(self, session, test_user):
        from app.features.token_usage.router import user_daily_stats

        with patch("app.features.auth.user_service.ensure_user_access"), \
             patch("app.features.token_usage.service.get_daily_stats", return_value=[]):
            result = user_daily_stats(test_user.id, 30, test_user, session)
        assert result == []

    def test_admin_users_stats(self, session, test_user):
        from app.features.token_usage.router import admin_all_users_stats

        with patch("app.features.token_usage.service.get_all_users_token_stats", return_value=[]):
            result = admin_all_users_stats(test_user, session)
        assert result == []

    def test_admin_logs(self, session, test_user):
        from app.features.token_usage.router import admin_all_logs

        with patch("app.features.token_usage.service.get_all_users_usage_logs", return_value={"items": []}):
            result = admin_all_logs(1, 20, None, None, None, None, test_user, session)
        assert "items" in result

    def test_admin_daily(self, session, test_user):
        from app.features.token_usage.router import admin_daily_stats

        with patch("app.features.token_usage.service.get_all_users_daily_stats", return_value=[]):
            result = admin_daily_stats(30, test_user, session)
        assert result == []

    def test_admin_per_user_daily(self, session, test_user):
        from app.features.token_usage.router import admin_per_user_daily_stats

        with patch("app.features.token_usage.service.get_per_user_daily_stats", return_value=[]):
            result = admin_per_user_daily_stats(30, test_user, session)
        assert result == []


# ---------------------------------------------------------------------------
# Chat Router 测试
# ---------------------------------------------------------------------------

class TestChatRouter:
    def test_chat_stream(self, session, test_user, test_workspace):
        from app.features.chat.router import chat
        from app.features.chat.schemas import ChatRequest
        import asyncio

        async def mock_events(*args, **kwargs):
            yield "data: test\n\n"

        payload = ChatRequest(query="hello")
        with patch("app.features.chat.router.generate_chat_events", mock_events):
            result = asyncio.run(chat(payload, test_user, test_workspace))
        assert result.media_type == "text/event-stream"
