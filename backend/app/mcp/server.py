"""MCP 协议适配层：将云盘能力暴露为 tools / resources / prompts。

职责边界：只做协议适配与鉴权注入，业务逻辑在 app.services。
独立容器运行；JWT 经 ASGI 中间件写入 ContextVar，工具侧无需手传 user_id。

约束：
1. 同步 DB 调用须经 _run_sync / to_thread，并在同一线程 SessionLocal() + close()
2. service 的 DomainError / HTTPException 转为结构化 JSON 错误，避免 MCP 客户端拿到裸栈
3. 工具参数与名称保持稳定，供 Claude Desktop / Cursor / OpenCode 长期配置
"""

import asyncio
import base64
import binascii
import json
import mimetypes
import os
from contextvars import ContextVar
from datetime import timedelta
from typing import Any

import jwt
from fastapi import HTTPException
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from app.exceptions import DomainError, PermissionDeniedError
from app.infra.datetime_utils import beijing_now
from app.infra.extensions import SECRET_KEY, SessionLocal
from app.infra.storage import get_storage_client
from app.infra.upload_adapter import Base64UploadAdapter
from app.features.auth.service import decode_token
from app.features.file import service as file_service
from app.features.folder import service as folder_service
from app.features.share import service as share_service
from app.features.workspace.permissions import get_member_role
from app.mcp.context import (
    McpRequestContext,
    get_optional_mcp_context,
    is_mcp_request_active,
    reset_mcp_context,
    reset_mcp_request_active,
    reset_request_id,
    set_mcp_context,
    set_mcp_request_active,
    set_request_id,
)
from app.mcp import runtime_token_service
from app.mcp.audit import audited_tool

from loguru import logger

MCP_INLINE_UPLOAD_MAX_BYTES = 8 * 1024 * 1024

# ---------------------------------------------------------------------------
# 请求级认证上下文
# ---------------------------------------------------------------------------
_current_user_id: ContextVar[int | None] = ContextVar("_current_user_id", default=None)


# ---------------------------------------------------------------------------
# ASGI 认证中间件
# ---------------------------------------------------------------------------
class JWTAuthMiddleware:
    """Resolve a verified user + workspace context for every MCP request.

    A normal session/MCP token selects a workspace through
    ``X-SKYCloud-Workspace-Id`` and membership lookup.  A runtime token carries
    its workspace in signed claims, but the runtime and current membership are
    still checked in the database.
    """

    def __init__(self, app):
        self.app = app

    @staticmethod
    def _resolve_context(
            session,
            token: str,
            workspace_header: str | None,
    ) -> McpRequestContext | None:
        try:
            payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        except jwt.InvalidTokenError:
            return None

        if payload.get("type") == "mcp_runtime":
            validated = runtime_token_service.validate_runtime_token(session, token)
            if not validated:
                return None
            runtime_payload, runtime = validated
            try:
                workspace_id = int(runtime_payload["workspace_id"])
            except (KeyError, TypeError, ValueError):
                return None
            if workspace_header is not None:
                try:
                    if int(workspace_header) != workspace_id:
                        return None
                except (TypeError, ValueError):
                    return None
            return McpRequestContext(
                user_id=int(runtime.user_id),
                workspace_id=workspace_id,
                role=str(runtime_payload.get("role", "viewer")),
                runtime_id=int(runtime.id),
                actor_type="opencode",
            )

        result = decode_token(session, token)
        if not result or not str(result).isdigit() or workspace_header is None:
            return None
        try:
            user_id = int(result)
            workspace_id = int(workspace_header)
        except (TypeError, ValueError):
            return None

        role = get_member_role(session, workspace_id, user_id)
        if role is None:
            return None
        return McpRequestContext(
            user_id=user_id,
            workspace_id=workspace_id,
            role=role.value,
            actor_type="user",
        )

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)

        headers = {
            key.lower(): value
            for key, value in scope.get("headers", [])
        }
        auth_value = headers.get(b"authorization", b"").decode("utf-8", errors="ignore")
        workspace_header = headers.get(b"x-skycloud-workspace-id")
        workspace_header_value = (
            workspace_header.decode("utf-8", errors="ignore")
            if workspace_header is not None
            else None
        )

        user_id: int | None = None
        context: McpRequestContext | None = None

        if auth_value.lower().startswith("bearer "):
            token = auth_value.split(" ", 1)[1].strip()
            if token:
                session = SessionLocal()
                try:
                    context = self._resolve_context(
                        session, token, workspace_header_value
                    )
                    if context:
                        user_id = context.user_id
                    else:
                        # Preserve the legacy diagnostic user-id ContextVar for
                        # direct middleware callers. Real tool authorization
                        # remains blocked because request_active=True and no
                        # verified workspace context exists.
                        result = decode_token(session, token)
                        if result and str(result).isdigit():
                            user_id = int(result)
                finally:
                    session.close()

        request_active_ctx = set_mcp_request_active(True)
        token_ctx = _current_user_id.set(user_id)
        context_ctx = set_mcp_context(context)
        request_id_ctx = set_request_id(
            headers.get(b"x-request-id", b"").decode("utf-8", errors="ignore") or None
        )
        try:
            await self.app(scope, receive, send)
        finally:
            reset_request_id(request_id_ctx)
            reset_mcp_context(context_ctx)
            _current_user_id.reset(token_ctx)
            reset_mcp_request_active(request_active_ctx)


def get_mcp_app(mcp_instance: FastMCP):
    """包装 streamable HTTP ASGI 应用，挂上 JWT 中间件。"""
    raw_app = mcp_instance.streamable_http_app()
    return JWTAuthMiddleware(raw_app)


# ---------------------------------------------------------------------------
# 工具辅助
# ---------------------------------------------------------------------------
def _get_authenticated_user_id() -> int:
    """Return the authenticated user, preserving legacy direct-call tests."""

    return _get_tool_context().user_id


def _get_tool_context() -> McpRequestContext:
    """Get strict request context, with a non-HTTP compatibility fallback.

    Older unit callers invoke tool functions directly and only set
    ``_current_user_id``.  That fallback is deliberately disabled once the
    ASGI middleware marks a real MCP request active, so an HTTP request can
    never bypass workspace selection.
    """

    context = get_optional_mcp_context()
    if context is not None:
        return context
    if not is_mcp_request_active():
        user_id = _current_user_id.get()
        if user_id is not None:
            return McpRequestContext(
                user_id=user_id,
                workspace_id=user_id,
                role="admin",
            )
    raise PermissionError(
        "Unauthorized: Missing or invalid Authorization header and workspace context."
    )


def _require_read_context() -> McpRequestContext:
    context = _get_tool_context()
    return context


def _require_write_context() -> McpRequestContext:
    context = _get_tool_context()
    if context.role not in {"editor", "admin"}:
        raise PermissionError("Workspace is read-only")
    return context


def _require_admin_context() -> McpRequestContext:
    context = _get_tool_context()
    if context.role != "admin":
        raise PermissionError("Workspace administrator permission required")
    return context


def _get_authorized_file(session, context: McpRequestContext, file_id: int):
    """Use the shared authorization service for every real MCP request.

    A few legacy unit tests call tool functions directly with a mocked session
    and only populate ``uploader_id``. That compatibility branch is never
    reachable through ``JWTAuthMiddleware``.
    """

    if not is_mcp_request_active() and get_optional_mcp_context() is None:
        file_obj = file_service.get_file(session, file_id)
        file_workspace_id = getattr(file_obj, "workspace_id", None)
        if isinstance(file_workspace_id, int) and file_workspace_id != context.workspace_id:
            raise PermissionDeniedError("Permission denied")
        if getattr(file_obj, "uploader_id", None) != context.user_id:
            raise PermissionDeniedError("Permission denied")
        return file_obj
    return file_service.get_authorized_file(
        session,
        workspace_id=context.workspace_id,
        user_id=context.user_id,
        file_id=file_id,
    )


def _get_authorized_folder(session, context: McpRequestContext, folder_id: int):
    """Compatibility equivalent for direct legacy folder-tool tests."""

    if not is_mcp_request_active() and get_optional_mcp_context() is None:
        folder = folder_service.get_folder(session, folder_id)
        folder_workspace_id = getattr(folder, "workspace_id", None)
        if isinstance(folder_workspace_id, int) and folder_workspace_id != context.workspace_id:
            raise PermissionDeniedError("Permission denied")
        folder_user_id = getattr(folder, "user_id", None)
        if isinstance(folder_user_id, int) and folder_user_id != context.user_id:
            raise PermissionDeniedError("Permission denied")
        return folder
    return folder_service.get_authorized_folder(
        session,
        context.workspace_id,
        context.user_id,
        folder_id,
    )


async def _run_sync(fn, *args, **kwargs):
    """在线程池执行同步函数；同一线程内 SessionLocal()，结束后 close。

    fn 的第一个参数须为 session（与 service 签名一致）。
    """

    def _work():
        session = SessionLocal()
        try:
            return fn(session, *args, **kwargs)
        finally:
            session.close()

    return await asyncio.to_thread(_work)


def _error_json(msg: str) -> str:
    """MCP 工具统一错误载荷。"""
    return json.dumps({"error": msg}, ensure_ascii=False)


def _service_error_json(exc: Exception) -> str:
    """将 service 层 DomainError / HTTPException 转为 MCP 错误 JSON。"""
    if isinstance(exc, DomainError):
        return _error_json(str(exc))
    if isinstance(exc, HTTPException):
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        return _error_json(detail)
    return _error_json(str(exc))


# ---------------------------------------------------------------------------
# FastMCP 实例
# ---------------------------------------------------------------------------
mcp = FastMCP(
    "SKYCloud",
    instructions=(
        "SKYCloud 是一个智能云盘系统。你可以通过以下工具来搜索文件、浏览文件夹、"
        "获取文件信息、创建文件夹、移动/重命名文件、删除文件、"
        "读取文本文件内容，以及使用 upload_file 直接上传文件内容到云盘。"
        "上传不依赖 curl。\n"
        "所有操作需要在连接时提供有效的 JWT Bearer Token 进行身份认证，"
        "无需手动传递 user_id。"
    ),
)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
@audited_tool("get_current_user")
async def get_current_user() -> str:
    """获取当前已认证用户的基本信息，包括用户名、角色、Token 用量统计等。"""
    context = _require_read_context()
    user_id = context.user_id

    def _work():
        session = SessionLocal()
        try:
            from app.models.user import User
            user = session.get(User, user_id)
            if not user:
                return _error_json("User not found")
            info = user.to_dict()
            info.pop("password_hash", None)  # 绝不经 MCP 回传哈希
            info["workspace_id"] = context.workspace_id
            info["workspace_role"] = context.role
            if context.runtime_id is not None:
                info["runtime_id"] = context.runtime_id
            return json.dumps(info, ensure_ascii=False, default=str)
        finally:
            session.close()

    return await asyncio.to_thread(_work)


@mcp.tool()
@audited_tool("search_files")
async def search_files(
        query: str,
        page: int = 1,
        page_size: int = 10,
        search_type: str = "fuzzy",
) -> str:
    """搜索用户的文件。

    Args:
        query: 搜索关键词
        page: 页码（默认 1）
        page_size: 每页条数（默认 10）
        search_type: 搜索类型。
            - "fuzzy"：仅对文件名进行模糊匹配，不搜索文件内容或描述。
            - "vector"：AI 语义搜索，可匹配文件内容和描述。
              如果需要按文件内容查找，请使用 vector 模式。
    """
    context = _require_read_context()
    # service 内部已 to_thread；session 在 await 完成后再 close
    session = SessionLocal()
    try:
        result = await file_service.search_files(
            session, context.workspace_id, query, page, page_size, search_type
        )
        return json.dumps(result, ensure_ascii=False, default=str)
    finally:
        session.close()


@mcp.tool()
@audited_tool("list_files")
async def list_files(
        parent_id: int | None = None,
        page: int = 1,
        page_size: int = 20,
        name: str | None = None,
        sort_by: str = "created_at",
        order: str = "desc",
) -> str:
    """列出指定目录下的文件和文件夹。

    Args:
        parent_id: 父文件夹 ID（None 表示根目录）
        page: 页码（默认 1）
        page_size: 每页条数（默认 20）
        name: 按名称过滤（可选）
        sort_by: 排序字段，可选 "name"、"size"、"created_at"（默认）
        order: 排序方向，"asc" 或 "desc"（默认）
    """
    context = _require_read_context()

    def _work():
        session = SessionLocal()
        try:
            resolved_parent_id = parent_id
            # None 表示「用户根目录内容」，需解析真实 root id，避免列出伪根自身
            if resolved_parent_id is None:
                resolved_parent_id = folder_service.get_root_folder_id(
                    session, context.workspace_id
                )
            return file_service.get_files_and_folders(
                session,
                context.workspace_id,
                resolved_parent_id,
                page,
                page_size,
                name,
                sort_by,
                order,
            )
        finally:
            session.close()

    result = await asyncio.to_thread(_work)
    return json.dumps(result, ensure_ascii=False, default=str)


@mcp.tool()
@audited_tool("get_file_info")
async def get_file_info(file_id: int) -> str:
    """获取单个文件的详细信息（名称、大小、类型、状态、描述等）。

    Args:
        file_id: 文件 ID
    """
    context = _require_read_context()

    def _work():
        session = SessionLocal()
        try:
            file_obj = _get_authorized_file(session, context, file_id)
            return json.dumps(file_obj.to_dict(), ensure_ascii=False, default=str)
        except (DomainError, HTTPException) as e:
            return _service_error_json(e)
        finally:
            session.close()

    return await asyncio.to_thread(_work)


@mcp.tool()
@audited_tool("create_folder")
async def create_folder(
        name: str,
        parent_id: int | None = None,
) -> str:
    """创建一个新文件夹。

    Args:
        name: 文件夹名称
        parent_id: 父文件夹 ID（None 表示在根目录下创建）
    """
    context = _require_write_context()

    def _work():
        session = SessionLocal()
        try:
            # 与 list_files 一致：避免 parent_id=None 造出第二套「伪根」
            resolved_parent_id = parent_id
            if resolved_parent_id is None:
                resolved_parent_id = folder_service.get_root_folder_id(
                    session, context.workspace_id
                )
            else:
                _get_authorized_folder(session, context, resolved_parent_id)

            folder = folder_service.create_folder(
                session,
                {
                    "name": name,
                    "workspace_id": context.workspace_id,
                    "parent_id": resolved_parent_id,
                },
            )
            return json.dumps(folder.to_dict(), ensure_ascii=False, default=str)
        except (DomainError, HTTPException) as e:
            return _service_error_json(e)
        finally:
            session.close()

    return await asyncio.to_thread(_work)


@mcp.tool()
@audited_tool("move_file")
async def move_file(
        file_id: int,
        new_name: str | None = None,
        new_parent_id: int | None = None,
) -> str:
    """移动或重命名文件。至少提供 new_name 或 new_parent_id 之一。

    Args:
        file_id: 文件 ID
        new_name: 新文件名（可选）
        new_parent_id: 新的父文件夹 ID（可选，用于移动文件）
    """
    context = _require_write_context()

    def _work():
        session = SessionLocal()
        try:
            _get_authorized_file(session, context, file_id)

            update_data: dict[str, Any] = {}
            if new_name is not None:
                update_data["name"] = new_name
            if new_parent_id is not None:
                _get_authorized_folder(session, context, new_parent_id)
                update_data["parent_id"] = new_parent_id

            if not update_data:
                return _error_json("Please provide new_name or new_parent_id")

            updated = file_service.update_file(session, file_id, update_data)
            return json.dumps(updated.to_dict(), ensure_ascii=False, default=str)
        except (DomainError, HTTPException) as e:
            return _service_error_json(e)
        finally:
            session.close()

    return await asyncio.to_thread(_work)


@mcp.tool()
@audited_tool("delete_file")
async def delete_file(file_id: int) -> str:
    """删除一个文件。此操作不可恢复。

    Args:
        file_id: 文件 ID
    """
    context = _require_write_context()

    def _work():
        session = SessionLocal()
        try:
            _get_authorized_file(session, context, file_id)

            file_service.delete_file(session, file_id)
            return json.dumps(
                {"success": True, "message": f"File {file_id} deleted"},
                ensure_ascii=False,
            )
        except (DomainError, HTTPException) as e:
            return _service_error_json(e)
        finally:
            session.close()

    return await asyncio.to_thread(_work)


# ---------------------------------------------------------------------------
# 文本可读性判定（read_file_content）
# ---------------------------------------------------------------------------
_TEXT_MIME_PREFIXES: tuple[str, ...] = (
    "text/",
)
_TEXT_MIME_EXACT: set[str] = {
    "application/json",
    "application/xml",
    "application/javascript",
    "application/x-yaml",
    "application/yaml",
    "application/toml",
    "application/x-sh",
    "application/sql",
    "application/x-python",
    "application/xhtml+xml",
}
# mime 常被标成 octet-stream，扩展名作兜底
_TEXT_EXTENSIONS: set[str] = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl",
    ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".c", ".cpp",
    ".h", ".hpp", ".go", ".rs", ".rb", ".php", ".sh", ".bash",
    ".sql", ".html", ".htm", ".css", ".scss", ".less", ".vue",
    ".log", ".env", ".gitignore", ".dockerfile",
}

_MAX_READ_BYTES = 512 * 1024  # 限制上下文体积，超大文件截断


def _is_text_file(mime_type: str | None, filename: str | None) -> bool:
    """MIME 或扩展名命中文本白名单则可直接读内容。"""
    if mime_type:
        mt = mime_type.lower()
        if any(mt.startswith(p) for p in _TEXT_MIME_PREFIXES):
            return True
        if mt in _TEXT_MIME_EXACT:
            return True
    if filename:
        _, ext = os.path.splitext(filename)
        if ext.lower() in _TEXT_EXTENSIONS:
            return True
    return False


@mcp.tool()
@audited_tool("get_file_download_url")
async def get_file_download_url(
        file_id: int,
        expires_hours: int = 24,
) -> str:
    """生成文件的临时下载链接（通过分享链接实现，自带过期时间）。

    返回的链接可以直接在浏览器中打开下载。

    Args:
        file_id: 文件 ID
        expires_hours: 链接有效时长，单位小时（默认 24，最大 168）
    """
    context = _require_read_context()

    def _work():
        session = SessionLocal()
        try:
            file_obj = _get_authorized_file(session, context, file_id)

            # 最长 7 天，防止永久分享链接被 MCP 客户端无意创建
            hours = max(1, min(expires_hours, 168))
            expires_at = beijing_now() + timedelta(hours=hours)

            share = share_service.create_share_link(
                session, context.user_id, file_id, expires_at
            )
            share_dict = share.to_dict()

            base_url = os.getenv("SKYCLOUD_BASE_URL", "http://localhost:5000")
            download_url = f"{base_url}/api/share/{share_dict['token']}"

            return json.dumps(
                {
                    "download_url": download_url,
                    "token": share_dict["token"],
                    "expires_at": share_dict.get("expires_at"),
                    "file_name": str(file_obj.name),
                    "file_size": file_obj.file_size,
                },
                ensure_ascii=False,
                default=str,
            )
        except (DomainError, HTTPException) as e:
            return _service_error_json(e)
        finally:
            session.close()

    return await asyncio.to_thread(_work)


@mcp.tool()
@audited_tool("read_file_content")
async def read_file_content(
        file_id: int,
        encoding: str = "utf-8",
) -> str:
    """读取文本文件的内容并返回。仅支持文本类文件（txt, md, csv, json, 代码文件等）。

    对于非文本文件（图片、视频、PDF 等），请使用 get_file_download_url 获取下载链接。
    内容最大返回 512KB，超出部分会被截断。

    Args:
        file_id: 文件 ID
        encoding: 文件编码（默认 utf-8）
    """
    context = _require_read_context()

    def _work():
        session = SessionLocal()
        try:
            file_obj = _get_authorized_file(session, context, file_id)

            if not _is_text_file(file_obj.mime_type, file_obj.name):
                return json.dumps(
                    {
                        "error": "Not a text file",
                        "mime_type": file_obj.mime_type,
                        "hint": "Use get_file_download_url for non-text files.",
                    },
                    ensure_ascii=False,
                )

            storage = get_storage_client()
            object_name = str(file_obj.file_path)
            if storage.file_exists(object_name):
                file_size = storage.get_file_size(object_name)
                truncated = file_size > _MAX_READ_BYTES

                # 下载到临时文件后读取文本内容
                import tempfile
                tmp_fd, tmp_path = tempfile.mkstemp()
                os.close(tmp_fd)
                try:
                    storage.download_file(object_name, tmp_path)
                    with open(tmp_path, "r", encoding=encoding, errors="replace") as f:
                        content = f.read(_MAX_READ_BYTES)
                finally:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
            elif not is_mcp_request_active() and get_optional_mcp_context() is None:
                # Compatibility for pre-object-storage direct unit tests. It
                # is intentionally unavailable through the HTTP MCP boundary.
                legacy_path = file_obj.get_abs_path()
                if not legacy_path or not os.path.exists(legacy_path):
                    return _error_json("File not found on server")
                file_size = os.path.getsize(legacy_path)
                truncated = file_size > _MAX_READ_BYTES
                with open(legacy_path, "r", encoding=encoding, errors="replace") as f:
                    content = f.read(_MAX_READ_BYTES)
            else:
                return _error_json("File not found on server")

            return json.dumps(
                {
                    "file_name": str(file_obj.name),
                    "file_size": file_size,
                    "truncated": truncated,
                    "encoding": encoding,
                    "content": content,
                },
                ensure_ascii=False,
                default=str,
            )
        except (DomainError, HTTPException) as e:
            return _service_error_json(e)
        finally:
            session.close()

    return await asyncio.to_thread(_work)


@mcp.tool()
@audited_tool("move_folder")
async def move_folder(
        folder_id: int,
        new_name: str | None = None,
        new_parent_id: int | None = None,
) -> str:
    """移动或重命名文件夹。至少提供 new_name 或 new_parent_id 之一。

    Args:
        folder_id: 文件夹 ID
        new_name: 新文件夹名称（可选）
        new_parent_id: 新的父文件夹 ID（可选，用于移动文件夹）
    """
    context = _require_write_context()

    def _work():
        session = SessionLocal()
        try:
            folder = _get_authorized_folder(session, context, folder_id)

            update_data: dict[str, Any] = {}
            if new_name is not None:
                update_data["name"] = new_name
            if new_parent_id is not None:
                _get_authorized_folder(session, context, new_parent_id)
                update_data["parent_id"] = new_parent_id

            if not update_data:
                return _error_json("Please provide new_name or new_parent_id")

            updated = folder_service.update_folder(session, folder_id, update_data)
            return json.dumps(updated.to_dict(), ensure_ascii=False, default=str)
        except (DomainError, HTTPException) as e:
            return _service_error_json(e)
        finally:
            session.close()

    return await asyncio.to_thread(_work)


@mcp.tool()
@audited_tool("delete_folder")
async def delete_folder(folder_id: int) -> str:
    """删除一个文件夹及其下所有内容（子文件夹和文件）。此操作不可恢复。

    Args:
        folder_id: 文件夹 ID
    """
    context = _require_write_context()

    def _work():
        session = SessionLocal()
        try:
            folder = _get_authorized_folder(session, context, folder_id)

            folder_name = folder.name
            folder_service.delete_folder(session, folder_id)
            return json.dumps(
                {"success": True, "message": f"Folder '{folder_name}' (ID: {folder_id}) deleted"},
                ensure_ascii=False,
            )
        except (DomainError, HTTPException) as e:
            return _service_error_json(e)
        finally:
            session.close()

    return await asyncio.to_thread(_work)


@mcp.tool()
@audited_tool("get_storage_overview")
async def get_storage_overview() -> str:
    """获取用户的云盘存储概览，包括文件总数、各状态文件数、总存储大小等。"""
    context = _require_read_context()

    def _work():
        session = SessionLocal()
        try:
            from sqlalchemy import func
            from app.models.file import File
            from app.models.folder import Folder

            file_stats = (
                session.query(
                    func.count(File.id).label("total_files"),
                    func.coalesce(func.sum(File.file_size), 0).label("total_size"),
                )
                .filter(File.workspace_id == context.workspace_id)
                .first()
            )
            status_rows = (
                session.query(File.status, func.count(File.id))
                .filter(File.workspace_id == context.workspace_id)
                .group_by(File.status)
                .all()
            )
            status_counts = dict(status_rows)

            folder_count = (
                               session.query(func.count(Folder.id))
                               .filter(Folder.workspace_id == context.workspace_id)
                               .scalar()
                           ) or 0

            total_size = int(file_stats.total_size) if file_stats else 0

            def _human_size(size_bytes: int) -> str:
                for unit in ("B", "KB", "MB", "GB", "TB"):
                    if abs(size_bytes) < 1024:
                        return f"{size_bytes:.1f} {unit}"
                    size_bytes /= 1024
                return f"{size_bytes:.1f} PB"

            return json.dumps(
                {
                    "total_files": file_stats.total_files if file_stats else 0,
                    "total_folders": folder_count,
                    "total_size_bytes": total_size,
                    "total_size_human": _human_size(total_size),
                    "status_breakdown": {
                        "success": status_counts.get("success", 0),
                        "pending": status_counts.get("pending", 0),
                        "processing": status_counts.get("processing", 0),
                        "fail": status_counts.get("fail", 0),
                    },
                },
                ensure_ascii=False,
                default=str,
            )
        finally:
            session.close()

    return await asyncio.to_thread(_work)


class BatchDeleteItem(BaseModel):
    """批量删除操作中的单个项目。"""
    id: int = Field(description="文件或文件夹的 ID")
    is_folder: bool = Field(default=False, description="是否为文件夹。true 表示文件夹，false 表示文件")


@mcp.tool()
@audited_tool("batch_delete")
async def batch_delete(
        items: list[BatchDeleteItem],
) -> str:
    """批量删除多个文件和/或文件夹。

    Args:
        items: 要删除的项目列表，每项包含 id（文件或文件夹ID）和 is_folder（是否为文件夹）
    """
    context = _require_write_context()

    def _work():
        session = SessionLocal()
        try:
            deleted = []
            errors = []
            for item in items:
                item_id = item.id
                is_folder = item.is_folder
                try:
                    if is_folder:
                        _get_authorized_folder(session, context, item_id)
                        folder_service.delete_folder(session, item_id)
                    else:
                        _get_authorized_file(session, context, item_id)
                        file_service.delete_file(session, item_id)
                    deleted.append(item_id)
                except (DomainError, HTTPException) as e:
                    if isinstance(e, DomainError):
                        errors.append({"id": item_id, "error": str(e)})
                    else:
                        detail = e.detail if isinstance(e.detail, str) else str(e.detail)
                        errors.append({"id": item_id, "error": detail})

            return json.dumps(
                {
                    "deleted_count": len(deleted),
                    "deleted_ids": deleted,
                    "errors": errors,
                },
                ensure_ascii=False,
            )
        finally:
            session.close()

    return await asyncio.to_thread(_work)


@mcp.tool()
@audited_tool("get_folder_tree")
async def get_folder_tree(max_depth: int = 3) -> str:
    """获取用户的文件夹树形结构，用于了解云盘的整体目录布局。

    Args:
        max_depth: 最大递归深度（默认 3，最大 10）
    """
    context = _require_read_context()

    def _work():
        session = SessionLocal()
        try:
            from app.models.folder import Folder

            depth = max(1, min(max_depth, 10))
            all_folders = (
                session.query(Folder)
                .filter(Folder.workspace_id == context.workspace_id)
                .all()
            )

            children_map: dict[int | None, list] = {}
            for f in all_folders:
                pid = f.parent_id
                if pid not in children_map:
                    children_map[pid] = []
                children_map[pid].append(f)

            def _build_tree(parent_id: int | None, current_depth: int) -> list[dict]:
                if current_depth > depth:
                    return []
                nodes = []
                for f in children_map.get(parent_id, []):
                    node: dict[str, Any] = {
                        "id": f.id,
                        "name": f.name,
                    }
                    sub = _build_tree(f.id, current_depth + 1)
                    if sub:
                        node["children"] = sub
                    nodes.append(node)
                return nodes

            tree = _build_tree(None, 1)
            return json.dumps(tree, ensure_ascii=False, default=str)
        finally:
            session.close()

    return await asyncio.to_thread(_work)


@mcp.tool()
@audited_tool("get_upload_url")
async def get_upload_url(parent_id: int | None = None) -> str:
    """获取文件上传所需的 API 地址和认证信息。

    返回上传 URL、认证 Token 和兼容旧客户端的 curl 示例。
    OpenCode Runtime 应优先使用 upload_file，不依赖 curl。
    上传后文件会自动进行 AI 处理（描述生成、向量索引）。

    Args:
        parent_id: 目标文件夹 ID（None 表示上传到根目录）
    """
    context = _require_write_context()

    # 无需 DB：仅拼装上传指引
    in_docker = os.path.exists("/.dockerenv")
    if in_docker:
        api_base = "http://skycloud-backend-api:5000"
    else:
        api_base = os.getenv("SKYCLOUD_BASE_URL", "http://host.docker.internal:5000")
    upload_url = f"{api_base}/api/files"

    parent_flag = ""
    if parent_id is not None:
        parent_flag = f' -F "parent_id={parent_id}"'

    curl_example = (
        f'curl -X POST "{upload_url}" '
        f'-H "Authorization: Bearer $TOKEN" '
        f'-H "X-Workspace-Id: {context.workspace_id}" '
        f'-F "file=@/path/to/your/file"'
        f'{parent_flag}'
    )

    hint = (
        "$TOKEN 应设置为当前连接 SKYCLOUD MCP 时使用的 Bearer Token。"
        "可以从 /root/.config/opencode/opencode.json 中的 "
        'mcp.SKYCLOUD.headers.Authorization 字段读取。'
    )

    return json.dumps(
        {
            "upload_url": upload_url,
            "curl_example": curl_example,
            "hint": hint,
            "parent_id": parent_id,
            "workspace_id": context.workspace_id,
        },
        ensure_ascii=False,
    )


def _inline_upload_adapter(
    filename: str,
    content: str,
    content_encoding: str,
    mime_type: str | None,
) -> Base64UploadAdapter:
    """Build a bounded upload adapter from text or Base64 MCP content."""

    normalized_filename = (filename or "").strip()
    if not normalized_filename:
        raise ValueError("filename is required")
    if len(normalized_filename) > 255:
        raise ValueError("filename is too long")

    encoding = (content_encoding or "utf-8").strip().lower().replace("_", "-")
    if encoding in {"utf-8", "utf8", "text", "plain"}:
        raw = content.encode("utf-8")
        effective_mime = mime_type or mimetypes.guess_type(normalized_filename)[0] or "text/plain"
        encoded = base64.b64encode(raw).decode("ascii")
    elif encoding == "base64":
        compact = "".join((content or "").split())
        if compact.startswith("data:"):
            try:
                header, compact = compact.split(",", 1)
            except ValueError as exc:
                raise ValueError("content contains an invalid data URI") from exc
            data_mime = header[5:].split(";", 1)[0].strip()
        else:
            data_mime = None
        try:
            raw = base64.b64decode(compact, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("content must be valid Base64") from exc
        effective_mime = mime_type or data_mime or mimetypes.guess_type(normalized_filename)[0]
        effective_mime = effective_mime or "application/octet-stream"
        encoded = base64.b64encode(raw).decode("ascii")
    else:
        raise ValueError("content_encoding must be utf-8 or base64")

    if len(raw) > MCP_INLINE_UPLOAD_MAX_BYTES:
        raise ValueError(
            f"inline upload exceeds {MCP_INLINE_UPLOAD_MAX_BYTES // (1024 * 1024)} MB"
        )

    adapter = Base64UploadAdapter(
        f"data:{effective_mime};base64,{encoded}",
        filename=normalized_filename,
    )
    adapter.mimetype = effective_mime
    return adapter


@mcp.tool()
@audited_tool("upload_file")
async def upload_file(
        filename: str,
        content: str,
        content_encoding: str = "utf-8",
        parent_id: int | None = None,
        mime_type: str | None = None,
) -> str:
    """直接把文本或 Base64 文件内容上传到当前工作空间云盘。

    这是专家 Runtime 上传文件的首选工具，不需要 curl、外部 HTTP 请求或
    读取任何 Token。文本文件默认使用 utf-8；图片、PDF 等二进制文件请将
    content_encoding 设置为 base64。单次内联上传上限为 8 MB。

    Args:
        filename: 云盘中的文件名，例如 ``report.md``。
        content: 文件正文（文本或 Base64 字符串）。
        content_encoding: ``utf-8``（默认）或 ``base64``。
        parent_id: 目标文件夹 ID；不传则上传到根目录。
        mime_type: 可选 MIME 类型，例如 ``text/markdown``。
    """

    context = _require_write_context()
    try:
        adapter = _inline_upload_adapter(filename, content, content_encoding, mime_type)
    except (TypeError, UnicodeError, ValueError) as exc:
        return _error_json(str(exc))

    def _work(session):
        try:
            file_obj = file_service.create_uploaded_file(
                session,
                context.workspace_id,
                context.user_id,
                adapter,
                parent_id,
            )
            return {
                "success": True,
                "file": file_obj.to_dict(),
                "message": "文件已上传到 SKYCloud 云盘",
            }
        except (DomainError, HTTPException) as exc:
            return json.loads(_service_error_json(exc))

    try:
        result = await _run_sync(_work)
    except Exception as exc:
        return _service_error_json(exc)
    return json.dumps(result, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# Resources（只读资源 URI）
# ---------------------------------------------------------------------------

@mcp.resource("skycloud://folders")
async def get_user_folders() -> str:
    """获取当前认证用户的所有文件夹列表。"""
    context = _require_read_context()
    folders = await _run_sync(folder_service.get_folders, context.workspace_id)
    return json.dumps(folders, ensure_ascii=False, default=str)


@mcp.resource("skycloud://files/{file_id}")
async def get_user_file(file_id: int) -> str:
    """获取单个文件的元数据和描述信息。"""
    context = _require_read_context()

    def _work():
        session = SessionLocal()
        try:
            file_obj = _get_authorized_file(session, context, file_id)
            return json.dumps(file_obj.to_dict(), ensure_ascii=False, default=str)
        except (DomainError, HTTPException) as e:
            return _service_error_json(e)
        finally:
            session.close()

    return await asyncio.to_thread(_work)


# ---------------------------------------------------------------------------
# Prompts（引导客户端如何组合工具）
# ---------------------------------------------------------------------------

@mcp.prompt()
async def find_file(description: str) -> str:
    """根据用户的自然语言描述，帮助定位和查找文件。

    Args:
        description: 用户对想要查找的文件的描述
    """
    return (
        f"用户想要查找以下文件：{description}\n\n"
        "请按以下步骤操作：\n"
        "1. 首先使用 search_files 工具以 fuzzy 模式搜索关键词。\n"
        "2. 如果模糊搜索结果不理想，尝试使用 vector 模式进行语义搜索。\n"
        "3. 如果仍未找到，使用 list_files 工具逐层浏览文件夹查找。\n"
        "4. 找到文件后，使用 get_file_info 获取详细信息并展示给用户。"
    )


@mcp.prompt()
async def organize_workspace() -> str:
    """帮助用户整理和组织云盘中的文件结构。"""
    return (
        "请帮助用户整理云盘文件，按以下步骤操作：\n\n"
        "1. 使用 list_files 查看根目录下的所有文件和文件夹。\n"
        "2. 分析文件类型和命名模式，建议合理的分类方案。\n"
        "3. 根据用户的确认，使用 create_folder 创建分类文件夹。\n"
        "4. 使用 move_file 将文件移动到对应的文件夹中。\n"
        "5. 完成后再次列出文件结构，确认整理结果。\n\n"
        "建议的分类维度：文件类型（文档/图片/代码等）、项目、时间等。"
    )


@mcp.prompt()
async def summarize_file(file_id: int) -> str:
    """读取并总结指定文件的内容。

    Args:
        file_id: 要总结的文件 ID
    """
    return (
        f"请执行以下操作来总结文件（ID: {file_id}）：\n\n"
        "1. 使用 get_file_info 获取文件的基本信息。\n"
        "2. 如果是文本文件，使用 read_file_content 读取文件内容。\n"
        "3. 对文件内容进行总结，包括：\n"
        "   - 文件类型和大小\n"
        "   - 主要内容概述\n"
        "   - 关键信息和要点\n"
        "4. 如果是非文本文件，使用 get_file_download_url 生成下载链接，"
        "并告知用户该文件类型无法直接总结。"
    )


@mcp.prompt()
async def batch_download(search_query: str) -> str:
    """根据搜索条件批量生成文件下载链接。

    Args:
        search_query: 搜索关键词
    """
    return (
        f"用户想要批量下载与 '{search_query}' 相关的文件。\n\n"
        "请按以下步骤操作：\n"
        "1. 使用 search_files 搜索匹配的文件。\n"
        "2. 展示搜索结果列表，让用户确认需要下载的文件。\n"
        "3. 对用户选择的每个文件，使用 get_file_download_url 生成下载链接。\n"
        "4. 将所有下载链接整理成清单展示给用户。"
    )
