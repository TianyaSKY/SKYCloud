"""MinIO 对象存储客户端：封装文件上传/下载/删除/存在性检查。

全局单例 ``storage_client``，由 extensions 配置驱动。
分片上传临时文件仍走本地磁盘，合并后再推送到 MinIO。
"""

import io
import os
import tempfile
from typing import BinaryIO

from loguru import logger
from minio import Minio
from minio.error import S3Error

# ---------------------------------------------------------------------------
# 配置读取
# ---------------------------------------------------------------------------
MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "127.0.0.1:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "minioadmin")
MINIO_BUCKET = os.getenv("MINIO_BUCKET", "skycloud")
MINIO_SECURE = os.getenv("MINIO_SECURE", "false").lower() in ("true", "1", "yes")


class MinIOStorageClient:
    """MinIO 存储操作封装，提供与本地磁盘类似的 upload/download/delete 接口。"""

    def __init__(
        self,
        endpoint: str = MINIO_ENDPOINT,
        access_key: str = MINIO_ACCESS_KEY,
        secret_key: str = MINIO_SECRET_KEY,
        bucket: str = MINIO_BUCKET,
        secure: bool = MINIO_SECURE,
    ):
        self._client = Minio(
            endpoint,
            access_key=access_key,
            secret_key=secret_key,
            secure=secure,
        )
        self._bucket = bucket
        self._ensure_bucket()

    def _ensure_bucket(self) -> None:
        """启动时确保 bucket 存在，不存在则创建。"""
        try:
            if not self._client.bucket_exists(self._bucket):
                self._client.make_bucket(self._bucket)
                logger.info("已创建 MinIO bucket: {}", self._bucket)
        except S3Error as e:
            logger.error("MinIO bucket 初始化失败: {}", e)
            raise

    # ------------------------------------------------------------------
    # 核心操作
    # ------------------------------------------------------------------

    def upload_file(self, local_path: str, object_name: str) -> None:
        """将本地文件上传到 MinIO。"""
        try:
            self._client.fput_object(self._bucket, object_name, local_path)
            logger.debug("上传文件到 MinIO: {} -> {}", local_path, object_name)
        except S3Error as e:
            logger.error("MinIO 上传失败 [{}]: {}", object_name, e)
            raise

    def upload_data(self, data: bytes, object_name: str, content_type: str = "application/octet-stream") -> None:
        """将内存数据上传到 MinIO。"""
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
            logger.error("MinIO 数据上传失败 [{}]: {}", object_name, e)
            raise

    def upload_stream(self, stream: BinaryIO, object_name: str, length: int, content_type: str = "application/octet-stream") -> None:
        """将流式数据上传到 MinIO。"""
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
            logger.error("MinIO 流式上传失败 [{}]: {}", object_name, e)
            raise

    def download_file(self, object_name: str, local_path: str) -> None:
        """从 MinIO 下载文件到本地路径。"""
        try:
            self._client.fget_object(self._bucket, object_name, local_path)
            logger.debug("从 MinIO 下载: {} -> {}", object_name, local_path)
        except S3Error as e:
            logger.error("MinIO 下载失败 [{}]: {}", object_name, e)
            raise

    def get_file_stream(self, object_name: str):
        """获取文件流（用于 StreamingResponse），调用方负责关闭。"""
        try:
            return self._client.get_object(self._bucket, object_name)
        except S3Error as e:
            logger.error("MinIO 获取文件流失败 [{}]: {}", object_name, e)
            raise

    @staticmethod
    def close_file_stream(stream) -> None:
        """关闭下载流并归还 HTTP 连接，供流式响应结束后调用。"""
        try:
            stream.close()
            stream.release_conn()
        except Exception as exc:
            logger.warning("关闭 MinIO 下载流失败: {}", exc)

    def delete_file(self, object_name: str) -> None:
        """从 MinIO 删除对象。"""
        try:
            self._client.remove_object(self._bucket, object_name)
            logger.debug("从 MinIO 删除: {}", object_name)
        except S3Error as e:
            logger.error("MinIO 删除失败 [{}]: {}", object_name, e)
            raise

    def file_exists(self, object_name: str) -> bool:
        """检查对象是否存在。"""
        try:
            self._client.stat_object(self._bucket, object_name)
            return True
        except S3Error as e:
            if e.code == "NoSuchKey":
                return False
            logger.error("MinIO stat 失败 [{}]: {}", object_name, e)
            raise

    def get_file_size(self, object_name: str) -> int:
        """获取对象大小（字节）。"""
        try:
            stat = self._client.stat_object(self._bucket, object_name)
            return stat.size
        except S3Error as e:
            logger.error("MinIO 获取文件大小失败 [{}]: {}", object_name, e)
            raise

    def download_to_temp(self, object_name: str, suffix: str = "") -> str:
        """下载到临时文件并返回路径，供索引等需要本地读取的场景使用。

        调用方使用完毕后应自行删除临时文件。
        """
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=suffix)
        os.close(tmp_fd)
        try:
            self.download_file(object_name, tmp_path)
            return tmp_path
        except Exception:
            # 下载失败时清理临时文件
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise


# ---------------------------------------------------------------------------
# 全局单例（延迟初始化，避免 import 时即连接）
# ---------------------------------------------------------------------------
_storage_client: MinIOStorageClient | None = None


def get_storage_client() -> MinIOStorageClient:
    """获取全局 MinIO 存储客户端单例。"""
    global _storage_client
    if _storage_client is None:
        _storage_client = MinIOStorageClient()
        logger.info("MinIO 存储客户端已初始化: endpoint={}, bucket={}", MINIO_ENDPOINT, MINIO_BUCKET)
    return _storage_client

