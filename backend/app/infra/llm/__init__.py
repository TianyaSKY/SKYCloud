"""AI/LLM 基础能力：模型配置与 API 侧异步调用入口。"""

from app.infra.llm.config import (
    get_chat_model_config,
    get_embedding_model_config,
    get_rerank_model_config,
    get_rerank_top_k,
    get_vl_model_config,
    is_model_config_sys_dict_key,
)
def __getattr__(name):
    # Reading configuration must not initialize SDKs or pull in LangChain;
    # the model gateway and Docker controller use only settings.
    if name in {"chat_completion", "embed_texts", "record_llm_usage"}:
        from app.infra.llm import client
        return getattr(client, name)
    raise AttributeError(name)


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
