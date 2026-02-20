import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

# Directly import the target module file so this test can run
# without importing the full backend app package.
SERVICES_DIR = Path(__file__).resolve().parents[1] / "app" / "services"
if str(SERVICES_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICES_DIR))

from model_config import (  # type: ignore
    get_chat_model_config,
    get_embedding_model_config,
    get_rerank_model_config,
    get_rerank_top_k,
    get_vl_model_config,
    is_model_config_sys_dict_key,
)


class ModelConfigTestCase(unittest.TestCase):
    def test_model_config_defaults(self):
        with patch.dict(os.environ, {"DEFAULT_MODEL_PWD": "fallback-key"}, clear=True):
            chat = get_chat_model_config()
            emb = get_embedding_model_config()
            vl = get_vl_model_config()
            rerank = get_rerank_model_config()
            rerank_top_k = get_rerank_top_k()

        self.assertEqual(
            chat,
            {
                "api": "https://api.siliconflow.cn/v1",
                "key": "fallback-key",
                "model": "deepseek-ai/DeepSeek-V3.2",
            },
        )
        self.assertEqual(
            emb,
            {
                "api": "https://api.siliconflow.cn/v1",
                "key": "fallback-key",
                "model": "Qwen/Qwen3-Embedding-8B",
            },
        )
        self.assertEqual(
            vl,
            {
                "api": "https://api.siliconflow.cn/v1",
                "key": "fallback-key",
                "model": "Qwen/Qwen3-VL-30B-A3B-Instruct",
            },
        )
        self.assertEqual(
            rerank,
            {
                "api": "https://api.siliconflow.cn/v1",
                "key": "fallback-key",
                "model": "BAAI/bge-reranker-v2-m3",
            },
        )
        self.assertEqual(rerank_top_k, 8)

    def test_model_config_env_override(self):
        with patch.dict(
            os.environ,
            {
                "CHAT_API_URL": "https://chat.example.com/v1",
                "CHAT_API_KEY": "chat-key",
                "CHAT_API_MODEL": "chat-model",
                "EMB_API_URL": "https://emb.example.com/v1",
                "EMB_API_KEY": "emb-key",
                "EMB_MODEL_NAME": "emb-model",
                "VL_API_URL": "https://vl.example.com/v1",
                "VL_API_KEY": "vl-key",
                "VL_API_MODEL": "vl-model",
                "RERANK_API_URL": "https://rerank.example.com/v1",
                "RERANK_API_KEY": "rerank-key",
                "RERANK_MODEL": "rerank-model",
                "RERANK_TOP_K": "5",
            },
            clear=True,
        ):
            chat = get_chat_model_config()
            emb = get_embedding_model_config()
            vl = get_vl_model_config()
            rerank = get_rerank_model_config()
            rerank_top_k = get_rerank_top_k()

        self.assertEqual(
            chat,
            {
                "api": "https://chat.example.com/v1",
                "key": "chat-key",
                "model": "chat-model",
            },
        )
        self.assertEqual(
            emb,
            {
                "api": "https://emb.example.com/v1",
                "key": "emb-key",
                "model": "emb-model",
            },
        )
        self.assertEqual(
            vl,
            {
                "api": "https://vl.example.com/v1",
                "key": "vl-key",
                "model": "vl-model",
            },
        )
        self.assertEqual(
            rerank,
            {
                "api": "https://rerank.example.com/v1",
                "key": "rerank-key",
                "model": "rerank-model",
            },
        )
        self.assertEqual(rerank_top_k, 5)

    def test_model_config_supports_legacy_lowercase_env(self):
        with patch.dict(
            os.environ,
            {
                "chat_api_url": "https://legacy-chat.example.com/v1",
                "chat_api_key": "legacy-chat-key",
                "chat_api_model": "legacy-chat-model",
                "rerank_api_url": "https://legacy-rerank.example.com/v1",
                "rerank_api_key": "legacy-rerank-key",
                "rerank_model": "legacy-rerank-model",
                "rerank_top_k": "11",
            },
            clear=True,
        ):
            chat = get_chat_model_config()
            rerank = get_rerank_model_config()
            rerank_top_k = get_rerank_top_k()

        self.assertEqual(
            chat,
            {
                "api": "https://legacy-chat.example.com/v1",
                "key": "legacy-chat-key",
                "model": "legacy-chat-model",
            },
        )
        self.assertEqual(
            rerank,
            {
                "api": "https://legacy-rerank.example.com/v1",
                "key": "legacy-rerank-key",
                "model": "legacy-rerank-model",
            },
        )
        self.assertEqual(rerank_top_k, 11)

    def test_model_key_detection(self):
        self.assertTrue(is_model_config_sys_dict_key("chat_api_url"))
        self.assertTrue(is_model_config_sys_dict_key("CHAT_API_URL"))
        self.assertTrue(is_model_config_sys_dict_key(" emb_model_name "))
        self.assertTrue(is_model_config_sys_dict_key("RERANK_MODEL"))
        self.assertFalse(is_model_config_sys_dict_key("site_name"))


if __name__ == "__main__":
    unittest.main()
