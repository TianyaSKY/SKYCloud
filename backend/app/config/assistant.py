"""AI 助手特性开关与执行租约超时强类型配置."""

import os
from dataclasses import dataclass
from typing import Final

DEFAULT_ASSISTANT_V2_ENABLED: Final[bool] = True
DEFAULT_ASSISTANT_EXPERT_ENABLED: Final[bool] = True
DEFAULT_ASSISTANT_PERMISSION_UI_ENABLED: Final[bool] = True
DEFAULT_ASSISTANT_RUNTIME_LOCK_TTL: Final[int] = 3600
DEFAULT_ASSISTANT_ABANDONED_RUN_GRACE_SECONDS: Final[int] = 45
MIN_ABANDONED_GRACE_SECONDS: Final[int] = 30


def _read_bool(name: str, *, default: bool = True) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() not in {"0", "false", "no", "off"}


def _read_positive_int(name: str, default: int) -> int:
    val = os.getenv(name)
    if val is None or not val.strip():
        return default
    try:
        parsed = int(val.strip())
    except (TypeError, ValueError):
        return default
    else:
        return parsed if parsed > 0 else default


@dataclass(frozen=True, slots=True)
class AssistantConfig:
    """助手系统开关与超时策略."""

    v2_enabled: bool = DEFAULT_ASSISTANT_V2_ENABLED
    expert_enabled: bool = DEFAULT_ASSISTANT_EXPERT_ENABLED
    permission_ui_enabled: bool = DEFAULT_ASSISTANT_PERMISSION_UI_ENABLED
    runtime_lock_ttl: int = DEFAULT_ASSISTANT_RUNTIME_LOCK_TTL
    abandoned_run_grace_seconds: int = (
        DEFAULT_ASSISTANT_ABANDONED_RUN_GRACE_SECONDS
    )

    def __post_init__(self) -> None:
        """校验配置数值边界."""
        if self.runtime_lock_ttl <= 0:
            msg = "runtime_lock_ttl must be > 0"
            raise ValueError(msg)
        if self.abandoned_run_grace_seconds < MIN_ABANDONED_GRACE_SECONDS:
            msg = (
                f"abandoned_run_grace_seconds must be at least "
                f"{MIN_ABANDONED_GRACE_SECONDS}"
            )
            raise ValueError(msg)


def get_assistant_config() -> AssistantConfig:
    """从环境变量解析或使用默认助手配置."""
    return AssistantConfig(
        v2_enabled=_read_bool(
            "ASSISTANT_V2_ENABLED", default=DEFAULT_ASSISTANT_V2_ENABLED,
        ),
        expert_enabled=_read_bool(
            "ASSISTANT_EXPERT_ENABLED", default=DEFAULT_ASSISTANT_EXPERT_ENABLED,
        ),
        permission_ui_enabled=_read_bool(
            "ASSISTANT_PERMISSION_UI_ENABLED",
            default=DEFAULT_ASSISTANT_PERMISSION_UI_ENABLED,
        ),
        runtime_lock_ttl=_read_positive_int(
            "ASSISTANT_RUNTIME_LOCK_TTL", DEFAULT_ASSISTANT_RUNTIME_LOCK_TTL,
        ),
        abandoned_run_grace_seconds=max(
            MIN_ABANDONED_GRACE_SECONDS,
            _read_positive_int(
                "ASSISTANT_ABANDONED_RUN_GRACE_SECONDS",
                DEFAULT_ASSISTANT_ABANDONED_RUN_GRACE_SECONDS,
            ),
        ),
    )
