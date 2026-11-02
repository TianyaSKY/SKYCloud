"""文件发布流程的类型化入参与字节存储契约接口."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, TypedDict

if TYPE_CHECKING:
    from app.features.file.repository import FileRecordSpec
    from app.infra.upload_staging import StagedUpload


class UploadSource(Protocol):
    """遵循 ``save(path)`` 契约约定的上传输入适配器."""

    filename: str

    @property
    def mimetype(self) -> str | None:
        """返回上传请求中声明的 MIME 内容类型 (若提供)."""
        ...

    def save(self, destination: str) -> None:
        """将上传载荷写入 ``destination`` 目标路径."""
        ...


class UploadStorage(Protocol):
    """发布或补偿上传操作所需的对象存储基础操作接口."""

    def upload_file(self, local_path: str, object_name: str) -> None:
        """以唯一对象名称上传单个已暂存的对象数据."""
        ...

    def delete_file(self, object_name: str) -> None:
        """在上传失败补偿或清理流程中删除单个对象."""
        ...

    def file_exists(self, object_name: str) -> bool:
        """检查指定对象是否存在以用于秒传复用."""
        ...


@dataclass(frozen=True, slots=True, kw_only=True)
class UploadScope:
    """单次上传发布事务涉及的工作空间、操作人与目标父目录."""

    workspace_id: int
    uploader_id: int
    parent_id: int | None


@dataclass(frozen=True, slots=True, kw_only=True)
class StagedFile:
    """在暂存租约保护下的有界字节数据及派生元数据."""

    staging: StagedUpload
    name: str
    size: int
    mime_type: str | None
    content_hash: str


@dataclass(frozen=True, slots=True, kw_only=True)
class StoredUpload:
    """成功执行 PUT 的对象的持久化预留记录与文件规格."""

    reservation_id: str
    spec: FileRecordSpec


class PreflightData(TypedDict, total=False):
    """从上传请求中解析出的秒传预检参数."""

    filename: str
    total_size: int
    parent_id: int | None
    mime_type: str | None
    content_hash: str | None
