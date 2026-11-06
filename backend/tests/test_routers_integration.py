"""路由器测试：直接调用路由函数并 mock 依赖。"""
import asyncio
from contextlib import nullcontext
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import Request, Response
from sqlalchemy.orm import Session, sessionmaker

from app.api.dependencies import RequestContext
from app.features.file.schemas import FileListQuery, FileSearchQuery
from app.features.token_usage.schemas import (
    AdminAllLogsQuery,
    MyUsageLogsQuery,
    UserUsageLogsQuery,
)
from app.models.user import User
from app.models.workspace import Workspace


def _owned_factory(session: Session) -> Any:
    return sessionmaker(
        bind=session.get_bind(),
        join_transaction_mode="create_savepoint",
    )


def _request_context(user: User, session: Session) -> RequestContext:
    return RequestContext(user=user, session_factory=_owned_factory(session))


def _null_factory(session: Session) -> Any:
    return lambda: nullcontext(session)


# Auth Router 测试


class TestAuthRouter:
    def test_login(self, session: Session, test_user: User) -> None:
        from app.features.auth.router import login
        from app.features.auth.schemas import LoginRequest
        from app.features.auth.service import generate_token

        payload = LoginRequest(username="testuser", password="password123")
        with patch("app.features.auth.service.login", return_value={"token": generate_token(test_user.id), "user": {}}) as mock:
            res = cast(dict[str, Any], login(payload, Request({'type': 'http', 'headers': []}), Response(), _null_factory(session)))
            assert res['success'] is True
            data = cast(dict[str, Any], res['data'])
        assert "token" in data
        mock.assert_called_once()

    def test_register(self, session: Session) -> None:
        from app.features.auth.router import register
        from app.features.auth.schemas import RegisterRequest

        mock_user = MagicMock()
        mock_user.to_dict.return_value = {"id": 1, "username": "new"}
        payload = RegisterRequest(username="new", password="pass123")
        with patch("app.features.auth.service.register_user", return_value=mock_user):
            res = cast(dict[str, Any], register(payload, _null_factory(session)))
            assert res['success'] is True
            data = res['data']
        assert data == {"message": "User registered successfully", "user": mock_user.to_dict.return_value}

    def test_get_mcp_token(self, session: Session, test_user: User) -> None:
        from app.features.auth.router import get_mcp_token

        with patch("app.features.auth.service.get_mcp_token", return_value={"token": "mcp"}):
            res = cast(dict[str, Any], get_mcp_token(Response(), test_user, _null_factory(session)))
            assert res['success'] is True
            data = cast(dict[str, Any], res['data'])
        assert "token" in data

    def test_refresh_mcp_token(self, session: Session, test_user: User) -> None:
        from app.features.auth.router import refresh_mcp_token

        with patch("app.features.auth.service.refresh_mcp_token", return_value={"token": "new"}):
            res = cast(dict[str, Any], refresh_mcp_token(Response(), test_user, _null_factory(session)))
            assert res['success'] is True
            data = cast(dict[str, Any], res['data'])
        assert "token" in data

    def test_create_user(self, session: Session, admin_user: User) -> None:
        from app.features.auth.router import create_user
        from app.features.auth.schemas import UserCreateRequest

        mock_user = MagicMock()
        mock_user.to_dict.return_value = {"id": 2}
        payload = UserCreateRequest(username="created", password="pass")
        with patch("app.features.auth.user_service.create_user", return_value=mock_user):
            res = cast(dict[str, Any], create_user(payload, admin_user, _null_factory(session)))
            assert res['success'] is True
            data = res['data']
        assert data == mock_user.to_dict.return_value

    def test_get_user(self, session: Session, test_user: User) -> None:
        from app.features.auth.router import get_user

        mock_user = MagicMock()
        mock_user.to_dict.return_value = {"id": test_user.id}
        with patch("app.features.auth.user_service.ensure_user_access"), \
             patch("app.features.auth.user_service.get_user", new_callable=AsyncMock, return_value=mock_user):
            res = cast(dict[str, Any], asyncio.run(get_user(user_id=test_user.id, current_user=test_user, session_factory=_owned_factory(session))))
            assert res['success'] is True
            data = cast(dict[str, Any], res['data'])
        assert "id" in data

    def test_update_user(self, session: Session, test_user: User) -> None:
        from app.features.auth.router import update_user
        from app.features.auth.schemas import UserUpdateRequest

        mock_user = MagicMock()
        mock_user.to_dict.return_value = {"id": test_user.id}
        payload = UserUpdateRequest(username="updated")
        with patch("app.features.auth.user_service.ensure_user_access"), \
             patch("app.features.auth.user_service.update_user", return_value=mock_user):
            res = cast(dict[str, Any], update_user(test_user.id, payload, test_user, _null_factory(session)))
            assert res['success'] is True
            data = cast(dict[str, Any], res['data'])
        assert "id" in data

    def test_update_password(self, session: Session, test_user: User) -> None:
        from app.features.auth.router import update_user_password
        from app.features.auth.schemas import UserPasswordUpdateRequest

        payload = UserPasswordUpdateRequest(old_password="old", new_password="new")
        with patch("app.features.auth.user_service.ensure_user_access"), \
             patch("app.features.auth.user_service.change_password"):
            res = cast(dict[str, Any], update_user_password(test_user.id, payload, test_user, _null_factory(session)))
            assert res['success'] is True
            data = cast(dict[str, Any], res['data'])
        assert "message" in data

    def test_delete_user(self, session: Session, test_user: User) -> None:
        from app.features.auth.router import delete_user

        with patch("app.features.auth.user_service.ensure_user_access"), \
             patch("app.features.auth.user_service.delete_user"):
            result = delete_user(test_user.id, test_user, _null_factory(session))
        assert result.status_code == 204


# Workspace Router 测试


class TestWorkspaceRouter:
    def test_create_workspace(self, session: Session, test_user: User) -> None:
        from app.features.workspace.router import create_workspace
        from app.features.workspace.schemas import WorkspaceCreateRequest

        mock_ws = MagicMock()
        mock_ws.to_dict.return_value = {"id": 1, "name": "new"}
        payload = WorkspaceCreateRequest(name="new", description=None)
        with patch("app.features.workspace.service.create_workspace", return_value=mock_ws):
            res = cast(dict[str, Any], create_workspace(payload, test_user, _null_factory(session)))
            assert res['success'] is True
            data = res['data']
        assert data == mock_ws.to_dict.return_value

    def test_list_workspaces(self, session: Session, test_user: User) -> None:
        from app.features.workspace.router import list_workspaces

        with patch("app.features.workspace.service.get_user_workspace_rows", return_value=[]):
            res = cast(dict[str, Any], list_workspaces(test_user, _null_factory(session)))
            assert res['success'] is True
            data = cast(dict[str, Any], res['data'])
        assert "workspaces" in data

    def test_get_workspace(self, session: Session, test_user: User, test_workspace: Workspace) -> None:
        from app.features.workspace.router import get_workspace

        with patch("app.features.workspace.permissions.assert_member"), \
             patch("app.features.workspace.service.get_workspace_detail", return_value={"id": 1}):
            res = cast(dict[str, Any], get_workspace(test_workspace.id, test_user, _null_factory(session)))
            assert res['success'] is True
            data = cast(dict[str, Any], res['data'])
        assert "id" in data

    def test_update_workspace(self, session: Session, test_user: User, test_workspace: Workspace) -> None:
        from app.features.workspace.router import update_workspace
        from app.features.workspace.schemas import WorkspaceUpdateRequest

        mock_ws = MagicMock()
        mock_ws.to_dict.return_value = {"id": test_workspace.id}
        payload = WorkspaceUpdateRequest(name="updated", description=None)
        with patch("app.features.workspace.service.update_workspace", return_value=mock_ws):
            res = cast(dict[str, Any], update_workspace(test_workspace.id, payload, test_user, _null_factory(session)))
            assert res['success'] is True
            data = cast(dict[str, Any], res['data'])
        assert "id" in data

    def test_delete_workspace(self, session: Session, test_user: User, test_workspace: Workspace) -> None:
        from app.features.workspace.router import delete_workspace

        with patch("app.features.workspace.service.delete_workspace"):
            res = cast(dict[str, Any], delete_workspace(test_workspace.id, test_user, _null_factory(session)))
            assert res['success'] is True
            data = res['data']
        assert data == {"message": "已删除"}

    def test_invite_member(self, session: Session, test_user: User, test_workspace: Workspace) -> None:
        from app.features.workspace.router import invite_member
        from app.features.workspace.schemas import MemberInviteRequest

        mock_member = MagicMock()
        mock_member.to_dict.return_value = {"user_id": 2}
        payload = MemberInviteRequest(username="new", role="editor")
        with patch("app.features.workspace.service.invite_member", return_value=mock_member):
            res = cast(dict[str, Any], invite_member(test_workspace.id, payload, test_user, _null_factory(session)))
            assert res['success'] is True
            data = res['data']
        assert data == mock_member.to_dict.return_value

    def test_list_members(self, session: Session, test_user: User, test_workspace: Workspace) -> None:
        from app.features.workspace.router import list_members

        with patch("app.features.workspace.permissions.assert_member"), \
             patch("app.features.workspace.service.list_members", return_value=[]):
            res = cast(dict[str, Any], list_members(test_workspace.id, test_user, _null_factory(session)))
            assert res['success'] is True
            data = cast(dict[str, Any], res['data'])
        assert "members" in data

    def test_update_member_role(self, session: Session, test_user: User, test_workspace: Workspace) -> None:
        from app.features.workspace.router import update_member_role
        from app.features.workspace.schemas import MemberRoleUpdateRequest

        mock_member = MagicMock()
        mock_member.to_dict.return_value = {"user_id": 2}
        payload = MemberRoleUpdateRequest(role="admin")
        with patch("app.features.workspace.service.update_member_role", return_value=mock_member):
            res = cast(dict[str, Any], update_member_role(test_workspace.id, 2, payload, test_user, _null_factory(session)))
            assert res['success'] is True
            data = cast(dict[str, Any], res['data'])
        assert "user_id" in data

    def test_remove_member(self, session: Session, test_user: User, test_workspace: Workspace) -> None:
        from app.features.workspace.router import remove_member

        with patch("app.features.workspace.service.remove_member"):
            res = cast(dict[str, Any], remove_member(test_workspace.id, 2, test_user, _null_factory(session)))
            assert res['success'] is True
            data = res['data']
        assert data == {"message": "已移除"}


# Folder Router 测试


class TestFolderRouter:
    def test_create_folder(self, session: Session, test_user: User, test_workspace: Workspace) -> None:
        from app.features.folder.router import create_folder
        from app.features.folder.schemas import FolderCreateRequest

        mock_folder = MagicMock()
        mock_folder.to_dict.return_value = {"id": 1}
        payload = FolderCreateRequest(name="new", parent_id=None)
        with patch("app.features.folder.service.create_folder", return_value=mock_folder):
            res = cast(dict[str, Any], create_folder(payload=payload, context=_request_context(test_user, session), workspace=test_workspace))
            assert res['success'] is True
            data = res['data']
        assert data == mock_folder.to_dict.return_value

    def test_update_folder(self, session: Session, test_user: User, test_workspace: Workspace) -> None:
        from app.features.folder.router import update_folder
        from app.features.folder.schemas import FolderUpdateRequest

        mock_folder = MagicMock()
        mock_folder.to_dict.return_value = {"id": 1}
        payload = FolderUpdateRequest(name="renamed")
        with patch("app.features.folder.service.get_authorized_folder"), \
             patch("app.features.folder.service.update_folder", return_value=mock_folder):
            res = cast(dict[str, Any], update_folder(folder_id=1, payload=payload, context=_request_context(test_user, session), workspace=test_workspace))
            assert res['success'] is True
            data = cast(dict[str, Any], res['data'])
        assert "id" in data

    def test_delete_folder(self, session: Session, test_user: User, test_workspace: Workspace) -> None:
        from app.features.folder.router import delete_folder

        with patch("app.features.folder.service.get_authorized_folder"), \
             patch("app.features.folder.service.delete_folder"):
            result = delete_folder(folder_id=1, context=_request_context(test_user, session), workspace=test_workspace)
        assert result.status_code == 204

    def test_get_root_folder_id(self, session: Session, test_user: User, test_workspace: Workspace) -> None:
        from app.features.folder.router import get_root_folder_id

        with patch("app.features.folder.service.get_root_folder_id", return_value=1):
            res = cast(dict[str, Any], get_root_folder_id(context=_request_context(test_user, session), workspace=test_workspace))
            assert res['success'] is True
            data = cast(dict[str, Any], res['data'])
        assert "root_folder_id" in data

    def test_get_folders(self, session: Session, test_user: User, test_workspace: Workspace) -> None:
        from app.features.folder.router import get_folders

        with patch("app.features.folder.service.get_folders", return_value=[]):
            res = cast(dict[str, Any], get_folders(context=_request_context(test_user, session), workspace=test_workspace))
            assert res['success'] is True
            data = cast(dict[str, Any], res['data'])
        assert "folders" in data

    def test_get_folder(self, session: Session, test_user: User, test_workspace: Workspace) -> None:
        from app.features.folder.router import get_folder

        mock_folder = MagicMock()
        mock_folder.to_dict.return_value = {"id": 1}
        with patch("app.features.folder.service.get_authorized_folder", return_value=mock_folder):
            res = cast(dict[str, Any], get_folder(folder_id=1, context=_request_context(test_user, session), workspace=test_workspace))
            assert res['success'] is True
            data = cast(dict[str, Any], res['data'])
        assert "id" in data

    def test_organize(self, session: Session, test_user: User, test_workspace: Workspace) -> None:
        from app.features.folder.router import organize_workspace_files
        from app.models.organize_task import OrganizeTask

        res = cast(dict[str, Any], organize_workspace_files(
            context=_request_context(test_user, session), workspace=test_workspace,
        ))

        assert res["success"] is True
        assert res["data"]["queued"] is True
        task = session.query(OrganizeTask).one()
        assert task.state == "queued" and task.user_id == test_user.id
        assert task.delivered_at is None


# File Router 测试


class TestFileRouter:
    def test_list_files(self, session: Session, test_user: User, test_workspace: Workspace) -> None:
        from app.features.file.router import list_files

        with patch("app.features.file.service.get_files_and_folders", return_value={"files": []}):
            res = cast(dict[str, Any], list_files(ctx=_request_context(test_user, session), workspace=test_workspace, query=FileListQuery(parent_id=None, page=1, page_size=10, name=None, sort_by="created_at", order="desc")))
            assert res['success'] is True
            data = cast(dict[str, Any], res['data'])
        assert "files" in data

    def test_search_files_empty(self, session: Session, test_user: User, test_workspace: Workspace) -> None:
        from app.features.file.router import search_files

        res = cast(dict[str, Any], asyncio.run(
            search_files(ctx=_request_context(test_user, session), workspace=test_workspace, query=FileSearchQuery(q="", page=1, page_size=10, type="fuzzy"))
        ))
        assert res['success'] is True
        data = cast(dict[str, Any], res['data'])
        assert data["total"] == 0

    def test_search_files(self, session: Session, test_user: User, test_workspace: Workspace) -> None:
        from app.features.file.router import search_files

        with patch("app.features.file.search_service.search_files", new_callable=AsyncMock, return_value={"items": []}):
            res = cast(dict[str, Any], asyncio.run(
                search_files(ctx=_request_context(test_user, session), workspace=test_workspace, query=FileSearchQuery(q="test", page=1, page_size=10, type="fuzzy"))
            ))
            assert res['success'] is True
            data = cast(dict[str, Any], res['data'])
        assert "items" in data

    def test_update_file(self, session: Session, test_user: User, test_workspace: Workspace) -> None:
        from app.features.file.router import update_file
        from app.features.file.schemas import FileUpdateRequest

        mock_file = MagicMock()
        mock_file.to_dict.return_value = {"id": 1}
        payload = FileUpdateRequest(name="renamed.txt")
        with patch("app.features.file.service.get_authorized_file"), \
             patch("app.features.file.service.update_file", return_value=mock_file):
            res = cast(dict[str, Any], update_file(file_id=1, payload=payload, ctx=_request_context(test_user, session), workspace=test_workspace))
            assert res['success'] is True
            data = cast(dict[str, Any], res['data'])
        assert "id" in data

    def test_delete_file(self, session: Session, test_user: User, test_workspace: Workspace) -> None:
        from app.features.file.router import delete_file

        with patch("app.features.file.service.get_authorized_file"), \
             patch("app.features.file.service.delete_file"):
            result = delete_file(file_id=1, ctx=_request_context(test_user, session), workspace=test_workspace)
        assert result.status_code == 204

    def test_get_file(self, session: Session, test_user: User, test_workspace: Workspace) -> None:
        from app.features.file.router import get_file

        mock_file = MagicMock()
        mock_file.to_dict.return_value = {"id": 1}
        with patch("app.features.file.service.get_authorized_file", return_value=mock_file):
            res = cast(dict[str, Any], get_file(file_id=1, ctx=_request_context(test_user, session), workspace=test_workspace))
            assert res['success'] is True
            data = cast(dict[str, Any], res['data'])
        assert "id" in data

    def test_download_file(self, session: Session, test_user: User, test_workspace: Workspace, tmp_path: Path) -> None:
        from app.features.file.router import download_file

        mock_file = MagicMock()
        mock_file.file_path = "test.txt"
        mock_file.name = "test.txt"
        mock_file.mime_type = "text/plain"
        with patch("app.features.file.service.get_downloadable_file", return_value=mock_file):
            result = download_file(file_id=1, ctx=_request_context(test_user, session), workspace=test_workspace)
        assert result.media_type == "text/plain"

    def test_batch_delete(self, session: Session, test_user: User, test_workspace: Workspace) -> None:
        from app.features.file.router import batch_delete_files
        from app.features.file.schemas import BatchDeleteItem, BatchDeleteRequest

        payload = BatchDeleteRequest(items=[BatchDeleteItem(id=1, is_folder=False)])
        with patch("app.features.file.service.batch_delete_items"):
            result = batch_delete_files(payload=payload, ctx=_request_context(test_user, session), workspace=test_workspace)
        assert result.status_code == 204

    def test_retry_embedding(self, session: Session, test_user: User, test_workspace: Workspace) -> None:
        from app.features.file.router import retry_embedding
        from app.features.file.schemas import RetryEmbeddingRequest

        mock_file = MagicMock()
        mock_file.id = 1
        payload = RetryEmbeddingRequest(file_id=1)
        with patch("app.features.file.service.get_authorized_file", return_value=mock_file), \
             patch("app.features.file.service.retry_embedding"):
            result = retry_embedding(payload=payload, ctx=_request_context(test_user, session), workspace=test_workspace)
        assert result.status_code == 204

    def test_rebuild_failed_indexes(self, session: Session, test_user: User, test_workspace: Workspace) -> None:
        from app.features.file.router import rebuild_failed_indexes

        with patch("app.features.file.service.rebuild_failed_indexes", return_value=5):
            res = cast(dict[str, Any], rebuild_failed_indexes(ctx=_request_context(test_user, session), workspace=test_workspace))
            assert res['success'] is True
            data = cast(dict[str, Any], res['data'])
        assert data["count"] == 5

    def test_process_status(self, session: Session, test_user: User, test_workspace: Workspace) -> None:
        from app.features.file.router import process_status

        with patch("app.features.file.service.process_status", new_callable=AsyncMock, return_value={"pending": 0}):
            res = cast(dict[str, Any], asyncio.run(process_status(ctx=_request_context(test_user, session), workspace=test_workspace)))
            assert res['success'] is True
            data = cast(dict[str, Any], res['data'])
        assert "pending" in data


# Share Router 测试


class TestShareRouter:
    def test_create_share(self, session: Session, test_user: User) -> None:
        from app.features.share.router import create_share
        from app.features.share.schemas import ShareCreateRequest

        mock_share = MagicMock()
        mock_share.to_dict.return_value = {"id": 1}
        payload = ShareCreateRequest(file_id=1)
        with patch("app.features.share.service.create_share", return_value=mock_share):
            result = create_share(payload, test_user, _null_factory(session))
        assert result == {"success": True, "data": {"id": 1}}

    def test_get_my_shares(self, session: Session, test_user: User) -> None:
        from app.features.share.router import get_my_shares

        with patch("app.features.share.service.get_my_shares", return_value=[]):
            result = get_my_shares(test_user, _null_factory(session))
        assert result == {"success": True, "data": []}

    def test_cancel_share(self, session: Session, test_user: User) -> None:
        from app.features.share.router import cancel_share

        with patch("app.features.share.service.cancel_share_for_user"):
            res = cast(dict[str, Any], cancel_share(1, test_user, _null_factory(session)))
        assert "message" in cast(dict[str, Any], res["data"])


# Inbox Router 测试


class TestInboxRouter:
    def test_get_inbox(self, session: Session, test_user: User) -> None:
        from app.features.inbox.router import get_inbox

        mock_result: dict[str, object] = {"items": [], "total": 0, "pages": 0, "page": 1}
        with patch("app.features.inbox.service.get_user_inbox", return_value=mock_result):
            res = cast(dict[str, Any], get_inbox(test_user, _null_factory(session), 1, 20))
        assert "items" in cast(dict[str, Any], res["data"])

    def test_mark_message_read(self, session: Session, test_user: User) -> None:
        from app.features.inbox.router import mark_message_read

        mock_msg = MagicMock()
        mock_msg.to_dict.return_value = {"id": 1}
        with patch("app.features.inbox.service.mark_as_read", return_value=mock_msg):
            res = cast(dict[str, Any], mark_message_read(1, test_user, _null_factory(session)))
        assert "id" in cast(dict[str, Any], res["data"])

    def test_mark_all_messages_read(self, session: Session, test_user: User) -> None:
        from app.features.inbox.router import mark_all_messages_read

        with patch("app.features.inbox.service.mark_all_as_read"):
            res = cast(dict[str, Any], mark_all_messages_read(test_user, _null_factory(session)))
        assert "message" in cast(dict[str, Any], res["data"])

    def test_delete_message(self, session: Session, test_user: User) -> None:
        from app.features.inbox.router import delete_message

        with patch("app.features.inbox.service.delete_inbox_message"):
            result = delete_message(1, test_user, _null_factory(session))
        assert result.status_code == 204


# SysDict Router 测试


class TestSysDictRouter:
    def test_get_sys_dicts(self, session: Session, admin_user: User) -> None:
        from app.features.sys_dict.router import get_sys_dicts

        with patch("app.features.sys_dict.service.get_sys_dict_all_sync", return_value=[]):
            result = get_sys_dicts(admin_user, _null_factory(session))
        assert result == {"success": True, "data": []}

    def test_get_sys_dict(self, session: Session, admin_user: User) -> None:
        from app.features.sys_dict.router import get_sys_dict

        with patch("app.features.sys_dict.service.get_sys_dict_sync", return_value={"id": 1}):
            res = cast(dict[str, Any], get_sys_dict(1, admin_user, _null_factory(session)))
        assert "id" in cast(dict[str, Any], res["data"])

    def test_create_sys_dict(self, session: Session, admin_user: User) -> None:
        from app.features.sys_dict.router import create_sys_dict
        from app.features.sys_dict.schemas import SysDictPayload

        mock_dict = MagicMock()
        mock_dict.to_dict.return_value = {"id": 1}
        payload = SysDictPayload(key="k", value="v")
        with patch("app.features.sys_dict.service.create_sys_dict", return_value=mock_dict):
            result = create_sys_dict(payload, admin_user, _null_factory(session))
        assert result == {"success": True, "data": {"id": 1}}

    def test_update_sys_dict(self, session: Session, admin_user: User) -> None:
        from app.features.sys_dict.router import update_sys_dict
        from app.features.sys_dict.schemas import SysDictPayload

        mock_dict = MagicMock()
        mock_dict.to_dict.return_value = {"id": 1}
        payload = SysDictPayload(key="k", value="v")
        with patch("app.features.sys_dict.service.update_sys_dict", return_value=mock_dict):
            res = cast(dict[str, Any], update_sys_dict(1, payload, admin_user, _null_factory(session)))
        assert "id" in cast(dict[str, Any], res["data"])

    def test_delete_sys_dict(self, session: Session, admin_user: User) -> None:
        from app.features.sys_dict.router import delete_sys_dict

        with patch("app.features.sys_dict.service.delete_sys_dict"):
            result = delete_sys_dict(1, admin_user, _null_factory(session))
        assert result.status_code == 204


# Token Usage Router 测试


class TestTokenUsageRouter:
    def test_my_stats(self, session: Session, test_user: User) -> None:
        from app.features.token_usage.router import my_token_stats

        with patch("app.features.token_usage.service.get_user_token_stats", return_value={"total": 100}):
            res = cast(dict[str, Any], my_token_stats(ctx=_request_context(test_user, session)))
        assert "total" in cast(dict[str, Any], res["data"])

    def test_user_stats(self, session: Session, test_user: User) -> None:
        from app.features.token_usage.router import user_token_stats

        with patch("app.features.auth.user_service.ensure_user_access"), \
             patch("app.features.token_usage.service.get_user_token_stats", return_value={}):
            result = user_token_stats(user_id=test_user.id, ctx=_request_context(test_user, session))
        assert result == {"success": True, "data": {}}

    def test_my_logs(self, session: Session, test_user: User) -> None:
        from app.features.token_usage.router import my_usage_logs

        with patch("app.features.token_usage.service.get_usage_logs", return_value={"items": []}):
            res = cast(dict[str, Any], my_usage_logs(ctx=_request_context(test_user, session), query=MyUsageLogsQuery(page=1, page_size=20, action=None, start_date=None, end_date=None)))
        assert "items" in cast(dict[str, Any], res["data"])

    def test_user_logs(self, session: Session, test_user: User) -> None:
        from app.features.token_usage.router import user_usage_logs

        with patch("app.features.auth.user_service.ensure_user_access"), \
             patch("app.features.token_usage.service.get_usage_logs", return_value={"items": []}):
            res = cast(dict[str, Any], user_usage_logs(user_id=test_user.id, ctx=_request_context(test_user, session), query=UserUsageLogsQuery(page=1, page_size=20, action=None, start_date=None, end_date=None)))
        assert "items" in cast(dict[str, Any], res["data"])

    def test_my_daily(self, session: Session, test_user: User) -> None:
        from app.features.token_usage.router import my_daily_stats

        with patch("app.features.token_usage.service.get_daily_stats", return_value=[]):
            result = my_daily_stats(ctx=_request_context(test_user, session), days=30)
        assert result == {"success": True, "data": []}

    def test_user_daily(self, session: Session, test_user: User) -> None:
        from app.features.token_usage.router import user_daily_stats

        with patch("app.features.auth.user_service.ensure_user_access"), \
             patch("app.features.token_usage.service.get_daily_stats", return_value=[]):
            result = user_daily_stats(user_id=test_user.id, ctx=_request_context(test_user, session), days=30)
        assert result == {"success": True, "data": []}

    def test_admin_users_stats(self, session: Session, admin_user: User) -> None:
        from app.features.token_usage.router import admin_all_users_stats

        with patch("app.features.token_usage.service.get_all_users_token_stats", return_value=[]):
            result = admin_all_users_stats(ctx=_request_context(admin_user, session))
        assert result == {"success": True, "data": []}

    def test_admin_logs(self, session: Session, admin_user: User) -> None:
        from app.features.token_usage.router import admin_all_logs

        with patch("app.features.token_usage.service.get_all_users_usage_logs", return_value={"items": []}):
            res = cast(dict[str, Any], admin_all_logs(ctx=_request_context(admin_user, session), query=AdminAllLogsQuery(page=1, page_size=20, action=None, user_id=None, start_date=None, end_date=None)))
        assert "items" in cast(dict[str, Any], res["data"])

    def test_admin_daily(self, session: Session, admin_user: User) -> None:
        from app.features.token_usage.router import admin_daily_stats

        with patch("app.features.token_usage.service.get_all_users_daily_stats", return_value=[]):
            result = admin_daily_stats(ctx=_request_context(admin_user, session), days=30)
        assert result == {"success": True, "data": []}

    def test_admin_per_user_daily(self, session: Session, admin_user: User) -> None:
        from app.features.token_usage.router import admin_per_user_daily_stats

        with patch("app.features.token_usage.service.get_per_user_daily_stats", return_value=[]):
            result = admin_per_user_daily_stats(ctx=_request_context(admin_user, session), days=30)
        assert result == {"success": True, "data": []}
