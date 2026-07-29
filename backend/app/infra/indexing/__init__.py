"""文件索引能力。

不要在包导入阶段加载 ``handler``：描述模块依赖 ``indexing.chunking``，而 handler
又依赖描述模块，提前导出 worker 函数会形成循环导入。
"""

from typing import Any

__all__ = ["handle_batch_indexing", "handle_file_indexing", "handle_file_process"]


def __getattr__(name: str) -> Any:
    """按需导出 Worker 入口，避免 chunking 子模块导入时循环依赖。"""
    if name in __all__:
        from app.infra.indexing import handler

        return getattr(handler, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
