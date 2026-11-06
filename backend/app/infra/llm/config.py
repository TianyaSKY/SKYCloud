"""从环境变量读取 Chat / Embedding / VL / Rerank 模型配置.

模型配置绝不能通过 sys_dict 进行持久化存储。
用户仅需配置 Chat 与 Embedding; VL 与 Rerank 支持智能降级与按需复用。
"""

import os
from typing import Final

from app.config.rag import get_rag_config

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
    },
)

DEFAULT_MODEL_API_BASE_URL: Final[str] = "https://api.siliconflow.cn/v1"
DEFAULT_CHAT_MODEL: Final[str] = "deepseek-ai/DeepSeek-V3.2"
DEFAULT_EMBEDDING_MODEL: Final[str] = "Qwen/Qwen3-Embedding-8B"
DEFAULT_VL_MODEL: Final[str] = "Qwen/Qwen3-VL-30B-A3B-Instruct"
DEFAULT_RERANK_MODEL: Final[str] = "BAAI/bge-reranker-v2-m3"
DEFAULT_RERANK_TOP_K: Final[int] = 8

# 常见已知具有视觉理解能力的多模态模型标识
_KNOWN_VISION_MODEL_SUBSTRINGS: Final[tuple[str, ...]] = (
    "vl",
    "vision",
    "-4o",
    "/gpt-4o",
    "gpt-4-turbo",
    "gpt-4-vision",
    "gpt-5.6-luna",
    "claude-3",
    "gemini-1.5",
    "gemini-2",
    "glm-4v",
    "-4v",
    "omni",
)


def is_model_config_sys_dict_key(key: str | None) -> bool:
    """识别是否为禁止写入 sys_dict 的模型配置键."""
    return (key or "").strip().lower() in MODEL_CONFIG_SYS_DICT_KEYS


def _read_env(*keys: str, default: str = "") -> str:
    # 兼容大小写变体和历史键名, 优先使用最新名称。
    for key in keys:
        value = os.getenv(key)
        if value is not None and value.strip() != "":
            return value.strip()
    return default


def chat_supports_vision(chat_config: dict[str, str] | str | None = None) -> bool:
    """检查当前 Chat 模型是否支持多模态视觉输入.

    不自动假定任意 Chat 模型都支持视觉。仅在显式声明或明确匹配已知视觉模型特征时复用。
    """
    explicit = os.getenv("CHAT_SUPPORTS_VISION")
    if explicit is not None and explicit.strip():
        return explicit.strip().lower() in {"1", "true", "yes", "on"}

    if isinstance(chat_config, str):
        model_name = chat_config.strip().lower()
    else:
        cfg = chat_config or get_chat_model_config()
        model_name = cfg.get("model", "").strip().lower()

    if not model_name:
        return False

    return any(sub in model_name for sub in _KNOWN_VISION_MODEL_SUBSTRINGS)


def get_chat_model_config() -> dict[str, str]:
    """返回聊天模型端点、API 密钥与模型名称."""
    return {
        "api": _read_env(
            "CHAT_API_URL",
            "chat_api_url",
            default=DEFAULT_MODEL_API_BASE_URL,
        ),
        "key": _read_env("CHAT_API_KEY", "chat_api_key", default=""),
        "model": _read_env(
            "CHAT_API_MODEL",
            "chat_api_model",
            default=DEFAULT_CHAT_MODEL,
        ),
    }


def get_embedding_model_config() -> dict[str, str]:
    """返回嵌入向量模型端点、API 密钥与模型名称."""
    return {
        "api": _read_env(
            "EMB_API_URL",
            "emb_api_url",
            default=DEFAULT_MODEL_API_BASE_URL,
        ),
        "key": _read_env("EMB_API_KEY", "emb_api_key", default=""),
        "model": _read_env(
            "EMB_MODEL_NAME",
            "emb_model_name",
            default=DEFAULT_EMBEDDING_MODEL,
        ),
    }


def get_vl_model_config() -> dict[str, str]:
    """返回视觉语言模型端点、API 密钥与模型名称.

    若提供了独立 VL 密钥则优先使用; 否则在 Chat 模型支持视觉时复用 Chat 模型;
    若均不可用则返回空密钥以触发降级处理。
    """
    vl_key = _read_env("VL_API_KEY", "vl_api_key", default="")
    if vl_key:
        return {
            "api": _read_env(
                "VL_API_URL",
                "vl_api_url",
                default=DEFAULT_MODEL_API_BASE_URL,
            ),
            "key": vl_key,
            "model": _read_env(
                "VL_API_MODEL", "vl_api_model", default=DEFAULT_VL_MODEL,
            ),
        }

    chat_cfg = get_chat_model_config()
    if chat_cfg.get("key") and chat_supports_vision(chat_cfg):
        return {
            "api": chat_cfg["api"],
            "key": chat_cfg["key"],
            "model": chat_cfg["model"],
        }

    return {
        "api": _read_env(
            "VL_API_URL",
            "vl_api_url",
            default=DEFAULT_MODEL_API_BASE_URL,
        ),
        "key": "",
        "model": _read_env("VL_API_MODEL", "vl_api_model", default=DEFAULT_VL_MODEL),
    }


def get_rerank_model_config() -> dict[str, str]:
    """返回重排模型端点、API 密钥与模型名称.

    没有单独提供重排模型密钥时返回空密钥, 检索链路将跳过重排并保留向量检索与 RRF 融合。
    """
    rerank_key = _read_env("RERANK_API_KEY", "rerank_api_key", default="")
    return {
        "api": _read_env(
            "RERANK_API_URL",
            "rerank_api_url",
            default=DEFAULT_MODEL_API_BASE_URL,
        ),
        "key": rerank_key,
        "model": _read_env(
            "RERANK_MODEL",
            "rerank_model",
            default=DEFAULT_RERANK_MODEL,
        ),
    }


def get_rerank_top_k() -> int:
    """返回配置的重排候选数量."""
    return get_rag_config().rerank_top_k
