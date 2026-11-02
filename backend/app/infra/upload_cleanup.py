"""尽最大努力清理分块部分文件, 绝不覆盖替换上传失败本身的异常."""

from pathlib import Path

from loguru import logger


def remove_partial_safely(path: str) -> None:
    """安全删除部分写入的文件; 绝不掩盖上传失败错误."""
    try:
        Path(path).unlink(missing_ok=True)
    except OSError as exc:
        # 拥有暂存/预留资源的清理机制可稍后重试删除。
        # 路径与操作系统错误信息可能包含隐私数据。
        logger.warning(
            "Partial upload cleanup deferred (error_type={})", type(exc).__name__,
        )
