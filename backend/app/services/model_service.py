"""远程 embedding 服务客户端（独立 LLM 进程）；连接失败返回 None 由调用方降级。"""

import logging
import os

import requests

LLM_SERVICE_URL = os.environ.get('LLM_SERVICE_URL', 'http://localhost:5001')

logger = logging.getLogger(__name__)


class RemoteEmbedder:
    def __init__(self, service_url):
        self.service_url = service_url

    def process(self, inputs):
        """调用远程 /embed；超时偏长以覆盖慢推理，失败返回 None。"""
        try:
            response = requests.post(
                f"{self.service_url}/embed",
                json={'inputs': inputs},
                timeout=300  # 模型推理可能较慢
            )
            response.raise_for_status()
            result = response.json()

            if 'embeddings' in result:
                # 远端返回 list，包装为 tensor 以兼容历史调用方
                import torch
                return torch.tensor(result['embeddings'])
            else:
                logger.error(f"Unexpected response from LLM service: {result}")
                return None

        except requests.exceptions.RequestException as e:
            logger.error(f"Error calling LLM service: {e}")
            return None


_embedder = None


def get_embedder():
    """懒加载全局 RemoteEmbedder 单例。"""
    global _embedder
    if _embedder is None:
        _embedder = RemoteEmbedder(LLM_SERVICE_URL)
    return _embedder
