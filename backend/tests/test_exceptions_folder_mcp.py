"""异常处理 / 文件夹服务 / MCP Token 服务测试。"""
from collections.abc import Callable
from datetime import timedelta
from typing import Any, cast
from unittest.mock import patch, MagicMock

import pytest
from fastapi import FastAPI
from sqlalchemy.orm import Session

from app.exceptions import (
    DomainError,
    ResourceNotFoundError,
    BusinessRuleError,
    PermissionDeniedError,
    PayloadTooLargeError,
    ConflictError,
    AuthenticationError,
    ServiceOperationError,
    register_exception_handlers,
    _to_json_safe,  # pyright: ignore[reportPrivateUsage]
)
from app.infra.datetime_utils import beijing_now
from app.models.folder import Folder
from app.models.file import File
from app.models.mcp_token import McpToken
from app.models.user import User
from app.models.workspace import Workspace


# 异常类层级


class TestDomainErrors:
    def test_status_codes(self) -> None:
        assert DomainError().status_code == 400
        assert ResourceNotFoundError().status_code == 404
        assert BusinessRuleError().status_code == 400
        assert PermissionDeniedError().status_code == 403
        assert PayloadTooLargeError().status_code == 413
        assert ConflictError().status_code == 409
        assert AuthenticationError().status_code == 401
        assert ServiceOperationError().status_code == 500

    def test_message(self) -> None:
        err = ResourceNotFoundError("文件不存在")
        assert str(err) == "文件不存在"


class TestToJsonSafe:
    def test_bytes(self) -> None:
        assert _to_json_safe(b"hello") == "hello"  # pyright: ignore[reportPrivateUsage]

    def test_tuple(self) -> None:
        assert _to_json_safe((1, "a")) == [1, "a"]  # pyright: ignore[reportPrivateUsage]

    def test_list(self) -> None:
        assert _to_json_safe([1, b"x"]) == [1, "x"]  # pyright: ignore[reportPrivateUsage]

    def test_dict(self) -> None:
        result = _to_json_safe({"key": b"val"})  # pyright: ignore[reportPrivateUsage]
        assert result == {"key": "val"}

    def test_plain_value(self) -> None:
        assert _to_json_safe(42) == 42  # pyright: ignore[reportPrivateUsage]
        assert _to_json_safe("str") == "str"  # pyright: ignore[reportPrivateUsage]


class TestExceptionHandlers:
    """直接调用异常处理器函数测试（避免 TestClient 版本兼容问题）。"""

    async def test_domain_error_handler(self) -> None:
        from fastapi import Request

        app = FastAPI()
        register_exception_handlers(app)

        # 找到注册的 handler
        handler = app.exception_handlers.get(DomainError)
        assert handler is not None

        exc = ResourceNotFoundError("未找到")
        mock_request = MagicMock(spec=Request)
        typed_handler = cast(Callable[[Request, DomainError], Any], handler)
        response = await typed_handler(mock_request, exc)
        assert response.status_code == 404
        assert response.body is not None

    async def test_http_exception_handler(self) -> None:
        from fastapi import HTTPException, Request

        app = FastAPI()
        register_exception_handlers(app)

        handler = app.exception_handlers.get(HTTPException)
        assert handler is not None

        exc = HTTPException(status_code=429, detail="Too many requests")
        mock_request = MagicMock(spec=Request)
        typed_handler = cast(Callable[[Request, HTTPException], Any], handler)
        response = await typed_handler(mock_request, exc)
        assert response.status_code == 429

    async def test_unexpected_exception_handler(self) -> None:
        from fastapi import Request

        app = FastAPI()
        register_exception_handlers(app)

        handler = app.exception_handlers.get(Exception)
        assert handler is not None

        exc = RuntimeError("boom")
        mock_request = MagicMock(spec=Request)
        typed_handler = cast(Callable[[Request, Exception], Any], handler)
        response = await typed_handler(mock_request, exc)
        assert response.status_code == 500


# 文件夹服务


class TestFolderService:
    def test_create_folder(self, session: Session, test_workspace: Workspace) -> None:
        from app.features.folder.service import FolderCreateData, create_folder

        root = session.query(Folder).filter_by(
            workspace_id=test_workspace.id, parent_id=None
        ).first()
        assert root is not None
        folder = create_folder(session, FolderCreateData(
            name="新目录",
            workspace_id=test_workspace.id,
            parent_id=root.id,
        ))
        assert folder.id is not None
        assert folder.name == "新目录"

    def test_get_folder(self, session: Session, test_folder: Folder) -> None:
        from app.features.folder.service import get_folder

        found = get_folder(session, test_folder.id)
        assert found.id == test_folder.id

    def test_get_folder_not_found(self, session: Session) -> None:
        from app.features.folder.service import get_folder
        from app.exceptions import ResourceNotFoundError

        with pytest.raises(ResourceNotFoundError):
            get_folder(session, 99999)

    def test_get_authorized_folder(self, session: Session, test_folder: Folder, test_workspace: Workspace, test_user: User) -> None:
        from app.features.folder.service import get_authorized_folder

        folder = get_authorized_folder(
            session, test_workspace.id, test_user.id, test_folder.id
        )
        assert folder.id == test_folder.id

    def test_get_authorized_folder_wrong_workspace(self, session: Session, test_folder: Folder, test_user: User) -> None:
        from app.features.folder.service import get_authorized_folder
        from app.exceptions import PermissionDeniedError

        with pytest.raises(PermissionDeniedError):
            get_authorized_folder(session, 99999, test_user.id, test_folder.id)

    def test_update_folder(self, session: Session, test_folder: Folder) -> None:
        from app.features.folder.service import FolderUpdateData, update_folder

        updated = update_folder(session, test_folder.id, FolderUpdateData(name="重命名"))
        assert updated.name == "重命名"

    def test_update_folder_not_found(self, session: Session) -> None:
        from app.features.folder.service import FolderUpdateData, update_folder
        from app.exceptions import ResourceNotFoundError

        with pytest.raises(ResourceNotFoundError):
            update_folder(session, 99999, FolderUpdateData(name="x"))

    def test_delete_folder(self, session: Session, test_folder: Folder, test_workspace: Workspace) -> None:
        from app.features.folder.service import delete_folder

        folder_id = test_folder.id
        with patch("app.features.folder.service.clear_search_cache"):
            delete_folder(session, folder_id)
        assert session.get(Folder, folder_id) is None

    def test_delete_folder_not_found(self, session: Session) -> None:
        from app.features.folder.service import delete_folder
        from app.exceptions import ResourceNotFoundError

        with pytest.raises(ResourceNotFoundError):
            delete_folder(session, 99999)

    def test_delete_folder_recursive(self, session: Session, test_workspace: Workspace, test_folder: Folder) -> None:
        from app.features.folder.service import delete_folder

        # 创建子文件夹和文件
        sub = Folder(name="子目录", workspace_id=test_workspace.id, parent_id=test_folder.id)
        session.add(sub)
        session.flush()

        f = File(
            name="file.txt", file_path="file.txt", file_size=10,
            workspace_id=test_workspace.id, parent_id=sub.id, status="pending",
        )
        session.add(f)
        session.flush()

        folder_id = test_folder.id
        sub_id = sub.id
        with patch("app.features.folder.service.clear_search_cache"), \
             patch("app.features.folder.service.delete_file"):
            delete_folder(session, folder_id)

        assert session.get(Folder, folder_id) is None
        assert session.get(Folder, sub_id) is None

    def test_get_root_folder_id(self, session: Session, test_workspace: Workspace) -> None:
        from app.features.folder.service import get_root_folder_id

        root_id = get_root_folder_id(session, test_workspace.id)
        assert root_id is not None

    def test_get_folders(self, session: Session, test_workspace: Workspace, test_folder: Folder) -> None:
        from app.features.folder.service import get_folders

        folders = get_folders(session, test_workspace.id)
        assert len(folders) >= 2  # 根 + 子目录


# MCP 令牌服务


class TestMcpTokenService:
    def test_get_active_record_none(self, session: Session, test_user: User) -> None:
        from app.mcp.token_service import get_active_record

        assert get_active_record(session, test_user.id) is None

    def test_get_active_record_exists(self, session: Session, test_user: User) -> None:
        from app.mcp.token_service import get_active_record

        token = McpToken(
            user_id=test_user.id,
            name="Test",
            token_hash="hash123",
            token_preview="abc...xyz",
            expires_at=beijing_now() + timedelta(days=30),
        )
        session.add(token)
        session.flush()

        record = get_active_record(session, test_user.id)
        assert record is not None
        assert record.id == token.id

    def test_ensure_user_mcp_token_creates(self, session: Session, test_user: User) -> None:
        from app.mcp.token_service import ensure_user_mcp_token

        record, raw = ensure_user_mcp_token(session, test_user.id)
        assert record is not None
        assert raw is not None
        assert record.user_id == test_user.id

    def test_ensure_user_mcp_token_reuses(self, session: Session, test_user: User) -> None:
        from app.mcp.token_service import ensure_user_mcp_token

        record1, raw1 = ensure_user_mcp_token(session, test_user.id)
        record2, raw2 = ensure_user_mcp_token(session, test_user.id)
        assert record1.id == record2.id
        assert raw1 and raw2 is None

    def test_refresh_user_mcp_token(self, session: Session, test_user: User) -> None:
        from app.mcp.token_service import ensure_user_mcp_token, refresh_user_mcp_token

        record1, raw1 = ensure_user_mcp_token(session, test_user.id)
        record2, raw2 = refresh_user_mcp_token(session, test_user.id)
        assert record2.id != record1.id
        assert raw2 != raw1
        # 旧 token 应被撤销
        session.refresh(record1)
        assert record1.revoked_at is not None

    def test_get_user_mcp_token_payload(self, session: Session, test_user: User) -> None:
        from app.mcp.token_service import get_user_mcp_token_payload

        payload = get_user_mcp_token_payload(session, test_user.id)
        assert "mcp_token" not in payload
        assert "token" in payload
        assert payload["user_id"] == test_user.id
        assert payload["expires_in_days"] == 365

    def test_get_active_mcp_token_valid(self, session: Session, test_user: User) -> None:
        from app.mcp.token_service import ensure_user_mcp_token, get_active_mcp_token

        _, raw = ensure_user_mcp_token(session, test_user.id)
        assert raw is not None
        record = get_active_mcp_token(session, raw)
        assert record is not None
        assert record.last_used_at is not None

    def test_get_active_mcp_token_invalid(self, session: Session) -> None:
        from app.mcp.token_service import get_active_mcp_token

        assert get_active_mcp_token(session, "invalid-token") is None

    def test_create_mcp_token_compat(self, session: Session, test_user: User) -> None:
        from app.mcp.token_service import create_mcp_token

        expires = beijing_now() + timedelta(days=30)
        record = create_mcp_token(session, test_user.id, "jwt-str", expires, "MyToken")
        assert record.name == "MyToken"
        assert record.user_id == test_user.id
