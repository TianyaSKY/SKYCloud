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
    get_vl_model_config,
    is_model_config_sys_dict_key,
)


class ModelConfigTestCase(unittest.TestCase):
    def test_model_config_defaults(self):
        with patch.dict(os.environ, {"DEFAULT_MODEL_PWD": "fallback-key"}, clear=True):
            chat = get_chat_model_config()
            emb = get_embedding_model_config()
            vl = get_vl_model_config()

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
            },
            clear=True,
        ):
            chat = get_chat_model_config()
            emb = get_embedding_model_config()
            vl = get_vl_model_config()

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

    def test_model_config_supports_legacy_lowercase_env(self):
        with patch.dict(
            os.environ,
            {
                "chat_api_url": "https://legacy-chat.example.com/v1",
                "chat_api_key": "legacy-chat-key",
                "chat_api_model": "legacy-chat-model",
            },
            clear=True,
        ):
            chat = get_chat_model_config()

        self.assertEqual(
            chat,
            {
                "api": "https://legacy-chat.example.com/v1",
                "key": "legacy-chat-key",
                "model": "legacy-chat-model",
            },
        )

    def test_model_key_detection(self):
        self.assertTrue(is_model_config_sys_dict_key("chat_api_url"))
        self.assertTrue(is_model_config_sys_dict_key("CHAT_API_URL"))
        self.assertTrue(is_model_config_sys_dict_key(" emb_model_name "))
        self.assertFalse(is_model_config_sys_dict_key("site_name"))


if __name__ == "__main__":
    unittest.main()
