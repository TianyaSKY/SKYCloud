"""MinIO 对象存储客户端: 封装文件上传/下载/删除/存在性检查.

全局单例 ``storage_client``, 由 extensions 配置驱动.
分片上传临时文件仍走本地磁盘, 合并后再推送到 MinIO.
"""

import io
import os
import tempfile
from pathlib import Path
from typing import BinaryIO

import urllib3
from loguru import logger
from minio import Minio
from minio.error import S3Error
from urllib3.response import BaseHTTPResponse

from app.infra.secrets import configured_password

# MinIO 对象存储配置读取
# 从环境变量读取端点、凭证、Bucket、安全标志与超时配置
# 有界 I/O: 设置连接与读取超时以防止工作线程阻塞
MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "127.0.0.1:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "")
MINIO_BUCKET = os.getenv("MINIO_BUCKET", "skycloud")
MINIO_SECURE = os.getenv("MINIO_SECURE", "false").lower() in ("true", "1", "yes")
# 有界 I/O: 无超时的对象存储调用会长期占用工作线程.
MINIO_CONNECT_TIMEOUT = float(os.getenv("MINIO_CONNECT_TIMEOUT", "5"))
MINIO_READ_TIMEOUT = float(os.getenv("MINIO_READ_TIMEOUT", "120"))
MINIO_RETRIES = int(os.getenv("MINIO_RETRIES", "2"))


class MinIOStorageClient:
    """MinIO 存储操作封装, 提供与本地磁盘类似的 upload/download/delete 接口."""

    def __init__(
        self,
        endpoint: str = MINIO_ENDPOINT,
        access_key: str = MINIO_ACCESS_KEY,
        secret_key: str = MINIO_SECRET_KEY,
        bucket: str = MINIO_BUCKET,
        *,
        secure: bool = MINIO_SECURE,
    ) -> None:
        """校验凭证有效性并连接有界 MinIO 客户端."""
        configured_password("MINIO_SECRET_KEY", secret_key)
        if not access_key:
            message = "MINIO_ACCESS_KEY must be explicitly configured"
            raise RuntimeError(message)
        self._client: Minio = Minio(
            endpoint,
            access_key=access_key,
            secret_key=secret_key,
            secure=secure,
            http_client=urllib3.PoolManager(
                timeout=urllib3.Timeout(
                    connect=MINIO_CONNECT_TIMEOUT, read=MINIO_READ_TIMEOUT,
                ),
                retries=urllib3.Retry(
                    total=MINIO_RETRIES,
                    backoff_factor=0.2,
                    status_forcelist=[500, 502, 503, 504],
                ),
            ),
        )
        self._bucket = bucket
        self._ensure_bucket()

    def _ensure_bucket(self) -> None:
        """启动时确保 bucket 存在, 不存在则创建."""
        try:
            if not self._client.bucket_exists(self._bucket):
                self._client.make_bucket(self._bucket)
                logger.info("已创建 MinIO bucket: {}", self._bucket)
        except S3Error as e:
            logger.error("MinIO bucket 初始化失败 (error_type={})", type(e).__name__)
            raise

    # MinIO 对象存储核心操作
    # 提供文件上传、下载、删除、元数据检索与存在性检查
    # 所有调用均受有界连接与读取超时保护

    def upload_file(self, local_path: str, object_name: str) -> None:
        """将本地文件上传到 MinIO."""
        try:
            self._client.fput_object(self._bucket, object_name, local_path)
            logger.debug("上传文件到 MinIO: {} -> {}", local_path, object_name)
        except S3Error as e:
            logger.error("MinIO 上传失败 (error_type={})", type(e).__name__)
            raise

    def upload_data(
        self,
        data: bytes,
        object_name: str,
        content_type: str = "application/octet-stream",
    ) -> None:
        """将内存数据上传到 MinIO."""
        try:
            self._client.put_object(
                self._bucket,
                object_name,
                io.BytesIO(data),
                length=len(data),
                content_type=content_type,
            )
            logger.debug("上传数据到 MinIO: {} ({} bytes)", object_name, len(data))
        except S3Error as e:
            logger.error("MinIO 数据上传失败 (error_type={})", type(e).__name__)
            raise

    def upload_stream(
        self,
        stream: BinaryIO,
        object_name: str,
        length: int,
        content_type: str = "application/octet-stream",
    ) -> None:
        """将流式数据上传到 MinIO."""
        try:
            self._client.put_object(
                self._bucket,
                object_name,
                stream,
                length=length,
                content_type=content_type,
            )
            logger.debug("流式上传到 MinIO: {} ({} bytes)", object_name, length)
        except S3Error as e:
            logger.error("MinIO 流式上传失败 (error_type={})", type(e).__name__)
            raise

    def download_file(self, object_name: str, local_path: str) -> None:
        """从 MinIO 下载文件到本地路径."""
        try:
            self._client.fget_object(self._bucket, object_name, local_path)
            logger.debug("从 MinIO 下载: {} -> {}", object_name, local_path)
        except S3Error as e:
            logger.error("MinIO 下载失败 (error_type={})", type(e).__name__)
            raise

    def get_file_stream(self, object_name: str) -> BaseHTTPResponse:
        """获取文件流用于 StreamingResponse, 调用方负责关闭."""
        try:
            return self._client.get_object(self._bucket, object_name)
        except S3Error as e:
            logger.error("MinIO 获取文件流失败 (error_type={})", type(e).__name__)
            raise

    def read_file_prefix(self, object_name: str, max_bytes: int) -> bytes:
        """将 HTTP 范围读取至有界内存中, 并始终释放其响应."""
        if max_bytes <= 0:
            message = "max_bytes must be positive"
            raise ValueError(message)
        response = self._client.get_object(
            self._bucket, object_name, length=max_bytes,
        )
        try:
            return response.read(max_bytes)
        finally:
            self.close_file_stream(response)

    @staticmethod
    def close_file_stream(stream: BaseHTTPResponse) -> None:
        """关闭下载流并归还 HTTP 连接, 供流式响应结束后调用."""
        # 即使关闭响应体失败, 也始终尝试归还底层 HTTP 连接。
        # 响应体投递可能出现任意网络异常, 失败类型集合是开放的。
        try:
            stream.close()
        except Exception as exc:
            logger.warning(
                "MinIO download stream cleanup failed (error_type={})",
                type(exc).__name__,
            )
        try:
            stream.release_conn()
        except Exception as exc:
            logger.warning(
                "MinIO download stream cleanup failed (error_type={})",
                type(exc).__name__,
            )

    def delete_file(self, object_name: str) -> None:
        """从 MinIO 删除对象."""
        try:
            self._client.remove_object(self._bucket, object_name)
            logger.debug("从 MinIO 删除: {}", object_name)
        except S3Error as e:
            logger.error("MinIO 删除失败 (error_type={})", type(e).__name__)
            raise

    def file_exists(self, object_name: str) -> bool:
        """检查对象是否存在."""
        try:
            self._client.stat_object(self._bucket, object_name)
        except S3Error as e:
            if e.code == "NoSuchKey":
                return False
            logger.error("MinIO stat 失败 (error_type={})", type(e).__name__)
            raise
        return True

    def get_file_size(self, object_name: str) -> int:
        """获取对象大小 (字节)."""
        try:
            stat = self._client.stat_object(self._bucket, object_name)
            size = stat.size
        except S3Error as e:
            logger.error("MinIO 获取文件大小失败 (error_type={})", type(e).__name__)
            raise
        if size is None:
            # 存在的对象始终包含大小信息; 缺失的对象绝不能
            # 被静默视为 0 字节。
            logger.error("MinIO stat 缺少对象大小 (error_type=ValueError)")
            message = "MinIO object size is unavailable"
            raise ValueError(message)
        return size

    def download_to_temp(self, object_name: str, suffix: str = "") -> str:
        """下载到临时文件并返回路径, 供索引等需要本地读取的场景使用.

        调用方使用完毕后应自行删除临时文件.
        """
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=suffix)
        os.close(tmp_fd)
        try:
            self.download_file(object_name, tmp_path)
        except Exception:
            # 下载失败时清理临时文件.任意存储失败都要清理后原样抛出.
            path = Path(tmp_path)
            if path.exists():
                path.unlink()
            raise
        return tmp_path


# 全局单例持有者
# 延迟初始化的进程级对象存储客户端单例容器
# 避免在模块导入时即发起 MinIO 网络连接
class _StorageClientHolder:
    """延迟初始化的进程级对象存储客户端单例容器."""

    client: MinIOStorageClient | None = None


_holder = _StorageClientHolder()


def get_storage_client() -> MinIOStorageClient:
    """获取全局 MinIO 存储客户端单例."""
    client = _holder.client
    if client is None:
        client = MinIOStorageClient()
        _holder.client = client
        logger.info(
            "MinIO 存储客户端已初始化: endpoint={}, bucket={}",
            MINIO_ENDPOINT,
            MINIO_BUCKET,
        )
    return client
