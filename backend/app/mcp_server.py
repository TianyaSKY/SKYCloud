"""
SKYCloud MCP Server
将云盘核心能力（文件搜索、文件夹浏览、文件管理）通过 MCP 协议对外暴露。

独立容器运行，复用 app.services 层，通过 JWT Token 认证用户身份。
"""
import json
import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from app.extensions import db
from app.services import file_service, folder_service
from app.services.auth_service import decode_token
from app.services import user_service

logger = logging.getLogger(__name__)

mcp = FastMCP(
    "SKYCloud",
    instructions=(
        "SKYCloud 是一个智能云盘系统。你可以通过以下工具来搜索文件、浏览文件夹、"
        "获取文件信息、创建文件夹、移动/重命名文件和删除文件。"
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
