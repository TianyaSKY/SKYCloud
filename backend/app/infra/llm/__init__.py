"""AI/LLM 基础能力：模型配置与 API 侧异步调用入口。"""

from app.infra.llm.config import (
    get_chat_model_config,
    get_embedding_model_config,
    get_rerank_model_config,
    get_rerank_top_k,
    get_vl_model_config,
    is_model_config_sys_dict_key,
)
from app.infra.llm.client import chat_completion, embed_texts, record_llm_usage

__all__ = [
    "get_chat_model_config",
    "get_embedding_model_config",
    "get_rerank_model_config",
    "get_rerank_top_k",
    "get_vl_model_config",
    "is_model_config_sys_dict_key",
    "chat_completion",
    "embed_texts",
    "record_llm_usage",
]
