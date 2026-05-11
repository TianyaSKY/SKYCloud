"""
SKYCloud MCP Server
将云盘核心能力（文件搜索、文件夹浏览、文件管理、文件下载/读取）通过 MCP 协议对外暴露。

独立容器运行，复用 app.services 层，通过 JWT Token 认证用户身份。

设计要点：
1. JWT 认证 — 通过 ASGI 中间件从 HTTP Authorization 头校验 token，
   自动将 user_id 注入 ContextVar，
   工具层通过 _get_authenticated_user_id() 获取，不再需要手动传 user_id。
2. 同步阻塞保护 — 所有同步 DB service 调用均通过 _run_sync() 卸载到线程池，
   避免阻塞事件循环，并保证 scoped_session 在执行线程上清理。
3. 异常处理 — 统一捕获 service 层抛出的 HTTPException，转为结构化 JSON 错误。
4. MCP Prompts — 实用 Prompt 模板，帮助 LLM 客户端更好地利用工具。
"""

import asyncio
import json
import logging
import os
from contextvars import ContextVar
from datetime import timedelta
from typing import Any

from fastapi import HTTPException
from mcp.server.fastmcp import FastMCP

from app.datetime_utils import beijing_now
from app.extensions import db
from app.services import file_service, folder_service, share_service
from app.services.auth_service import decode_token

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ContextVar: 每个请求的认证 user_id 通过此变量传递
# ---------------------------------------------------------------------------
_current_user_id: ContextVar[int | None] = ContextVar("_current_user_id", default=None)


# ---------------------------------------------------------------------------
# ASGI 认证中间件
# ---------------------------------------------------------------------------
class JWTAuthMiddleware:
    """
    ASGI 中间件，从 HTTP Authorization 头提取 Bearer token，
    调用 decode_token() 校验后将 user_id 注入 ContextVar。

    对非 MCP 路径（如 /health）以及 MCP 初始化握手请求不拦截。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)

        # 从 ASGI scope 获取 headers
        headers = dict(scope.get("headers", []))
        auth_value = headers.get(b"authorization", b"").decode("utf-8", errors="ignore")

        user_id: int | None = None

        if auth_value.lower().startswith("bearer "):
            token = auth_value.split(" ", 1)[1].strip()
            if token:
                result = decode_token(token)
                # decode_token 成功时返回 user_id（str），失败时返回错误信息字符串
                if result and str(result).isdigit():
                    user_id = int(result)

        # 将 user_id 注入 ContextVar
        token_ctx = _current_user_id.set(user_id)
        try:
            await self.app(scope, receive, send)
        finally:
            _current_user_id.reset(token_ctx)


def get_mcp_app(mcp_instance: FastMCP):
    """获取带有 JWT 认证中间件的 ASGI 应用。"""
    raw_app = mcp_instance.streamable_http_app()
    return JWTAuthMiddleware(raw_app)


# ---------------------------------------------------------------------------
# Helper: 获取当前请求的认证 user_id
# ---------------------------------------------------------------------------
def _get_authenticated_user_id() -> int:
    """从 ContextVar 中获取已认证的 user_id，未认证则抛出异常。"""
    user_id = _current_user_id.get()
    if user_id is None:
        raise PermissionError(
            "Unauthorized: Missing or invalid Authorization header. "
            "Please provide a valid Bearer token (JWT)."
        )
    return user_id


# ---------------------------------------------------------------------------
# Helper: 在线程池中执行同步函数，确保 scoped_session 在正确的线程上清理
# ---------------------------------------------------------------------------
async def _run_sync(fn, *args, **kwargs):
    """在线程池中执行同步函数。

    scoped_session 按线程 ID 管理 session，因此 db.session.remove() 必须
    在执行 DB 操作的同一线程上调用。本辅助函数将 fn 及 session 清理
    一起放入 to_thread，保证线程安全。
    """
    def _work():
        try:
            return fn(*args, **kwargs)
        finally:
            db.session.remove()
    return await asyncio.to_thread(_work)


def _error_json(msg: str) -> str:
    """返回统一格式的 JSON 错误字符串。"""
    return json.dumps({"error": msg}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 创建 FastMCP 实例
# ---------------------------------------------------------------------------
mcp = FastMCP(
    "SKYCloud",
    instructions=(
        "SKYCloud 是一个智能云盘系统。你可以通过以下工具来搜索文件、浏览文件夹、"
        "获取文件信息、创建文件夹、移动/重命名文件、删除文件、"
        "获取文件下载链接以及读取文本文件内容。\n"
        "所有操作需要在连接时提供有效的 JWT Bearer Token 进行身份认证，"
        "无需手动传递 user_id。"
    ),
)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
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
        search_type: 搜索类型，"fuzzy"（模糊匹配文件名）或 "vector"（AI 语义搜索）
    """
    user_id = _get_authenticated_user_id()
    # search_files 是 async 函数，内部已通过 to_thread 卸载同步 DB 操作
    result = await file_service.search_files(
        user_id, query, page, page_size, search_type
    )
    return json.dumps(result, ensure_ascii=False, default=str)


@mcp.tool()
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
    user_id = _get_authenticated_user_id()
    result = await _run_sync(
        file_service.get_files_and_folders,
        user_id, parent_id, page, page_size, name, sort_by, order,
    )
    return json.dumps(result, ensure_ascii=False, default=str)


@mcp.tool()
async def get_file_info(file_id: int) -> str:
    """获取单个文件的详细信息（名称、大小、类型、状态、描述等）。

    Args:
        file_id: 文件 ID
    """
    user_id = _get_authenticated_user_id()

    def _work():
        try:
            file_obj = file_service.get_file(file_id)
            if file_obj.uploader_id != user_id:
                return _error_json("Permission denied")
            return json.dumps(file_obj.to_dict(), ensure_ascii=False, default=str)
        except HTTPException as e:
            return _error_json(e.detail)
        finally:
            db.session.remove()

    return await asyncio.to_thread(_work)


@mcp.tool()
async def create_folder(
    name: str,
    parent_id: int | None = None,
) -> str:
    """创建一个新文件夹。

    Args:
        name: 文件夹名称
        parent_id: 父文件夹 ID（None 表示在根目录下创建）
    """
    user_id = _get_authenticated_user_id()

    def _work():
        try:
            folder = folder_service.create_folder(
                {"name": name, "user_id": user_id, "parent_id": parent_id},
            )
            return json.dumps(folder.to_dict(), ensure_ascii=False, default=str)
        except HTTPException as e:
            return _error_json(e.detail)
        finally:
            db.session.remove()

    return await asyncio.to_thread(_work)


@mcp.tool()
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
    user_id = _get_authenticated_user_id()

    def _work():
        try:
            file_obj = file_service.get_file(file_id)
            if file_obj.uploader_id != user_id:
                return _error_json("Permission denied")

            update_data: dict[str, Any] = {}
            if new_name is not None:
                update_data["name"] = new_name
            if new_parent_id is not None:
                update_data["parent_id"] = new_parent_id

            if not update_data:
                return _error_json("Please provide new_name or new_parent_id")

            updated = file_service.update_file(file_id, update_data)
            return json.dumps(updated.to_dict(), ensure_ascii=False, default=str)
        except HTTPException as e:
            return _error_json(e.detail)
        finally:
            db.session.remove()

    return await asyncio.to_thread(_work)


@mcp.tool()
async def delete_file(file_id: int) -> str:
    """删除一个文件。此操作不可恢复。

    Args:
        file_id: 文件 ID
    """
    user_id = _get_authenticated_user_id()

    def _work():
        try:
            file_obj = file_service.get_file(file_id)
            if file_obj.uploader_id != user_id:
                return _error_json("Permission denied")

            file_service.delete_file(file_id)
            return json.dumps(
                {"success": True, "message": f"File {file_id} deleted"},
                ensure_ascii=False,
            )
        except HTTPException as e:
            return _error_json(e.detail)
        finally:
            db.session.remove()

    return await asyncio.to_thread(_work)


# ---------------------------------------------------------------------------
# 文本类 MIME 前缀白名单，用于 read_file_content 判断是否可读
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
# 按扩展名的兜底：某些系统 mime_type 可能为 application/octet-stream
_TEXT_EXTENSIONS: set[str] = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl",
    ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".c", ".cpp",
    ".h", ".hpp", ".go", ".rs", ".rb", ".php", ".sh", ".bash",
    ".sql", ".html", ".htm", ".css", ".scss", ".less", ".vue",
    ".log", ".env", ".gitignore", ".dockerfile",
}

_MAX_READ_BYTES = 512 * 1024  # 最大读取 512KB，避免超大文件撑爆上下文


def _is_text_file(mime_type: str | None, filename: str | None) -> bool:
    """判断文件是否为可直接读取的文本文件。"""
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
    user_id = _get_authenticated_user_id()

    def _work():
        try:
            file_obj = file_service.get_file(file_id)
            if file_obj.uploader_id != user_id:
                return _error_json("Permission denied")

            # 限制最大过期时间为 7 天
            hours = max(1, min(expires_hours, 168))
            expires_at = beijing_now() + timedelta(hours=hours)

            share = share_service.create_share_link(user_id, file_id, expires_at)
            share_dict = share.to_dict()

            # 构建下载 URL：使用环境变量或默认值
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
        except HTTPException as e:
            return _error_json(e.detail)
        finally:
            db.session.remove()

    return await asyncio.to_thread(_work)


@mcp.tool()
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
    user_id = _get_authenticated_user_id()

    def _work():
        try:
            file_obj = file_service.get_file(file_id)
            if file_obj.uploader_id != user_id:
                return _error_json("Permission denied")

            # 判断是否为文本文件
            if not _is_text_file(file_obj.mime_type, file_obj.name):
                return json.dumps(
                    {
                        "error": "Not a text file",
                        "mime_type": file_obj.mime_type,
                        "hint": "Use get_file_download_url for non-text files.",
                    },
                    ensure_ascii=False,
                )

            abs_path = file_obj.get_abs_path()
            if not os.path.exists(abs_path):
                return _error_json("File not found on server")

            file_size = os.path.getsize(abs_path)
            truncated = file_size > _MAX_READ_BYTES

            with open(abs_path, "r", encoding=encoding, errors="replace") as f:
                content = f.read(_MAX_READ_BYTES)

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
        except HTTPException as e:
            return _error_json(e.detail)
        finally:
            db.session.remove()

    return await asyncio.to_thread(_work)


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------

@mcp.resource("skycloud://folders")
async def get_user_folders() -> str:
    """获取当前认证用户的所有文件夹列表。"""
    user_id = _get_authenticated_user_id()
    folders = await _run_sync(folder_service.get_folders, user_id)
    return json.dumps(folders, ensure_ascii=False, default=str)


@mcp.resource("skycloud://files/{file_id}")
async def get_user_file(file_id: int) -> str:
    """获取单个文件的元数据和描述信息。"""
    user_id = _get_authenticated_user_id()

    def _work():
        try:
            file_obj = file_service.get_file(file_id)
            if file_obj.uploader_id != user_id:
                return _error_json("Permission denied")
            return json.dumps(file_obj.to_dict(), ensure_ascii=False, default=str)
        except HTTPException as e:
            return _error_json(e.detail)
        finally:
            db.session.remove()

    return await asyncio.to_thread(_work)


# ---------------------------------------------------------------------------
# Prompts
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
