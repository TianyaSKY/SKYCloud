import os
from typing import Final

MODEL_CONFIG_SYS_DICT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "chat_api_url",
        "chat_api_key",
        "chat_api_model",
        "emb_api_url",
        "emb_api_key",
        "emb_model_name",
        "vl_api_url",
        "vl_api_key",
        "vl_api_model",
        "rerank_api_url",
        "rerank_api_key",
        "rerank_model",
    }
)

DEFAULT_MODEL_API_BASE_URL: Final[str] = "https://api.siliconflow.cn/v1"
DEFAULT_CHAT_MODEL: Final[str] = "deepseek-ai/DeepSeek-V3.2"
DEFAULT_EMBEDDING_MODEL: Final[str] = "Qwen/Qwen3-Embedding-8B"
DEFAULT_VL_MODEL: Final[str] = "Qwen/Qwen3-VL-30B-A3B-Instruct"
DEFAULT_RERANK_MODEL: Final[str] = "BAAI/bge-reranker-v2-m3"
DEFAULT_RERANK_TOP_K: Final[int] = 8


def is_model_config_sys_dict_key(key: str | None) -> bool:
    return (key or "").strip().lower() in MODEL_CONFIG_SYS_DICT_KEYS


def _read_env(*keys: str, default: str = "") -> str:
    for key in keys:
        value = os.getenv(key)
        if value is not None and value.strip() != "":
            return value.strip()
    return default


def _default_api_key() -> str:
    return _read_env("DEFAULT_MODEL_PWD", default="")


def _read_env_int(*keys: str, default: int) -> int:
    value = _read_env(*keys, default=str(default))
    try:
        parsed = int(value)
        return parsed if parsed > 0 else default
    except (TypeError, ValueError):
        return default


def get_chat_model_config() -> dict[str, str]:
    return {
        "api": _read_env(
            "CHAT_API_URL",
            "chat_api_url",
            default=DEFAULT_MODEL_API_BASE_URL,
        ),
        "key": _read_env("CHAT_API_KEY", "chat_api_key", default=_default_api_key()),
        "model": _read_env("CHAT_API_MODEL", "chat_api_model", default=DEFAULT_CHAT_MODEL),
    }


def get_embedding_model_config() -> dict[str, str]:
    return {
        "api": _read_env(
            "EMB_API_URL",
            "emb_api_url",
            default=DEFAULT_MODEL_API_BASE_URL,
        ),
        "key": _read_env("EMB_API_KEY", "emb_api_key", default=_default_api_key()),
        "model": _read_env(
            "EMB_MODEL_NAME",
            "emb_model_name",
            default=DEFAULT_EMBEDDING_MODEL,
        ),
    }


def get_vl_model_config() -> dict[str, str]:
    return {
        "api": _read_env(
            "VL_API_URL",
            "vl_api_url",
            default=DEFAULT_MODEL_API_BASE_URL,
        ),
        "key": _read_env("VL_API_KEY", "vl_api_key", default=_default_api_key()),
        "model": _read_env("VL_API_MODEL", "vl_api_model", default=DEFAULT_VL_MODEL),
    }


def get_rerank_model_config() -> dict[str, str]:
    return {
        "api": _read_env(
            "RERANK_API_URL",
            "rerank_api_url",
            default=DEFAULT_MODEL_API_BASE_URL,
        ),
        "key": _read_env("RERANK_API_KEY", "rerank_api_key", default=_default_api_key()),
        "model": _read_env("RERANK_MODEL", "rerank_model", default=DEFAULT_RERANK_MODEL),
    }


def get_rerank_top_k() -> int:
    return _read_env_int("RERANK_TOP_K", "rerank_top_k", default=DEFAULT_RERANK_TOP_K)
