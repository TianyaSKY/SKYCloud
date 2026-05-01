"""
SKYCloud MCP Server
将云盘核心能力（文件搜索、文件夹浏览、文件管理、文件下载/读取）通过 MCP 协议对外暴露。

独立容器运行，复用 app.services 层，通过 JWT Token 认证用户身份。
"""
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from mcp.server.fastmcp import FastMCP

from app.extensions import db
from app.services import file_service, folder_service, share_service
from app.services.auth_service import decode_token
from app.services import user_service

logger = logging.getLogger(__name__)

mcp = FastMCP(
    "SKYCloud",
    instructions=(
        "SKYCloud 是一个智能云盘系统。你可以通过以下工具来搜索文件、浏览文件夹、"
        "获取文件信息、创建文件夹、移动/重命名文件、删除文件、"
        "获取文件下载链接以及读取文本文件内容。"
        "所有操作都需要提供 user_id 来标识当前用户。"
    ),
)


# ---------------------------------------------------------------------------
# Helper: 确保 DB session 在每次 tool 调用后清理
# ---------------------------------------------------------------------------
def _cleanup_session() -> None:
    """移除当前线程绑定的 scoped_session，避免跨请求数据残留。"""
    try:
        db.session.remove()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def search_files(
    user_id: int,
    query: str,
    page: int = 1,
    page_size: int = 10,
    search_type: str = "fuzzy",
) -> str:
    """搜索用户的文件。

    Args:
        user_id: 用户 ID
        query: 搜索关键词
        page: 页码（默认 1）
        page_size: 每页条数（默认 10）
        search_type: 搜索类型，"fuzzy"（模糊匹配文件名）或 "vector"（AI 语义搜索）
    """
    try:
        result = await file_service.search_files(
            user_id, query, page, page_size, search_type
        )
        return json.dumps(result, ensure_ascii=False, default=str)
    finally:
        _cleanup_session()


@mcp.tool()
async def list_files(
    user_id: int,
    parent_id: int | None = None,
    page: int = 1,
    page_size: int = 20,
    name: str | None = None,
    sort_by: str = "created_at",
    order: str = "desc",
) -> str:
    """列出指定目录下的文件和文件夹。

    Args:
        user_id: 用户 ID
        parent_id: 父文件夹 ID（None 表示根目录）
        page: 页码（默认 1）
        page_size: 每页条数（默认 20）
        name: 按名称过滤（可选）
        sort_by: 排序字段，可选 "name"、"size"、"created_at"（默认）
        order: 排序方向，"asc" 或 "desc"（默认）
    """
    try:
        result = file_service.get_files_and_folders(
            user_id, parent_id, page, page_size, name, sort_by, order
        )
        return json.dumps(result, ensure_ascii=False, default=str)
    finally:
        _cleanup_session()


@mcp.tool()
async def get_file_info(user_id: int, file_id: int) -> str:
    """获取单个文件的详细信息（名称、大小、类型、状态、描述等）。

    Args:
        user_id: 用户 ID（用于权限验证）
        file_id: 文件 ID
    """
    try:
        file_obj = file_service.get_file(file_id)
        # 权限校验：文件必须属于当前用户
        if file_obj.uploader_id != user_id:
            return json.dumps({"error": "Permission denied"}, ensure_ascii=False)
        return json.dumps(file_obj.to_dict(), ensure_ascii=False, default=str)
    finally:
        _cleanup_session()


@mcp.tool()
async def create_folder(
    user_id: int, name: str, parent_id: int | None = None
) -> str:
    """创建一个新文件夹。

    Args:
        user_id: 用户 ID
        name: 文件夹名称
        parent_id: 父文件夹 ID（None 表示在根目录下创建）
    """
    try:
        folder = folder_service.create_folder(
            {"name": name, "user_id": user_id, "parent_id": parent_id}
        )
        return json.dumps(folder.to_dict(), ensure_ascii=False, default=str)
    finally:
        _cleanup_session()


@mcp.tool()
async def move_file(
    user_id: int,
    file_id: int,
    new_name: str | None = None,
    new_parent_id: int | None = None,
) -> str:
    """移动或重命名文件。至少提供 new_name 或 new_parent_id 之一。

    Args:
        user_id: 用户 ID（用于权限验证）
        file_id: 文件 ID
        new_name: 新文件名（可选）
        new_parent_id: 新的父文件夹 ID（可选，用于移动文件）
    """
    try:
        file_obj = file_service.get_file(file_id)
        if file_obj.uploader_id != user_id:
            return json.dumps({"error": "Permission denied"}, ensure_ascii=False)

        update_data: dict[str, Any] = {}
        if new_name is not None:
            update_data["name"] = new_name
        if new_parent_id is not None:
            update_data["parent_id"] = new_parent_id

        if not update_data:
            return json.dumps(
                {"error": "Please provide new_name or new_parent_id"},
                ensure_ascii=False,
            )

        updated = file_service.update_file(file_id, update_data)
        return json.dumps(updated.to_dict(), ensure_ascii=False, default=str)
    finally:
        _cleanup_session()


@mcp.tool()
async def delete_file(user_id: int, file_id: int) -> str:
    """删除一个文件。此操作不可恢复。

    Args:
        user_id: 用户 ID（用于权限验证）
        file_id: 文件 ID
    """
    try:
        file_obj = file_service.get_file(file_id)
        if file_obj.uploader_id != user_id:
            return json.dumps({"error": "Permission denied"}, ensure_ascii=False)

        file_service.delete_file(file_id)
        return json.dumps(
            {"success": True, "message": f"File {file_id} deleted"},
            ensure_ascii=False,
        )
    finally:
        _cleanup_session()


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
    user_id: int,
    file_id: int,
    expires_hours: int = 24,
) -> str:
    """生成文件的临时下载链接（通过分享链接实现，自带过期时间）。

    返回的链接可以直接在浏览器中打开下载。

    Args:
        user_id: 用户 ID（用于权限验证）
        file_id: 文件 ID
        expires_hours: 链接有效时长，单位小时（默认 24，最大 168）
    """
    try:
        file_obj = file_service.get_file(file_id)
        if file_obj.uploader_id != user_id:
            return json.dumps({"error": "Permission denied"}, ensure_ascii=False)

        # 限制最大过期时间为 7 天
        hours = max(1, min(expires_hours, 168))
        expires_at = datetime.now(timezone.utc) + timedelta(hours=hours)

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
    finally:
        _cleanup_session()


@mcp.tool()
async def read_file_content(
    user_id: int,
    file_id: int,
    encoding: str = "utf-8",
) -> str:
    """读取文本文件的内容并返回。仅支持文本类文件（txt, md, csv, json, 代码文件等）。

    对于非文本文件（图片、视频、PDF 等），请使用 get_file_download_url 获取下载链接。
    内容最大返回 512KB，超出部分会被截断。

    Args:
        user_id: 用户 ID（用于权限验证）
        file_id: 文件 ID
        encoding: 文件编码（默认 utf-8）
    """
    try:
        file_obj = file_service.get_file(file_id)
        if file_obj.uploader_id != user_id:
            return json.dumps({"error": "Permission denied"}, ensure_ascii=False)

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
            return json.dumps(
                {"error": "File not found on server"}, ensure_ascii=False
            )

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
    finally:
        _cleanup_session()


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------

@mcp.resource("skycloud://user/{user_id}/folders")
async def get_user_folders(user_id: int) -> str:
    """获取用户的所有文件夹列表。"""
    try:
        folders = await folder_service.get_folders(user_id)
        return json.dumps(folders, ensure_ascii=False, default=str)
    finally:
        _cleanup_session()


@mcp.resource("skycloud://user/{user_id}/files/{file_id}")
async def get_user_file(user_id: int, file_id: int) -> str:
    """获取单个文件的元数据和描述信息。"""
    try:
        file_obj = file_service.get_file(file_id)
        if file_obj.uploader_id != user_id:
            return json.dumps({"error": "Permission denied"}, ensure_ascii=False)
        return json.dumps(file_obj.to_dict(), ensure_ascii=False, default=str)
    finally:
        _cleanup_session()
