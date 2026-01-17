import logging
import os

import requests

# LLM 服务地址
LLM_SERVICE_URL = os.environ.get('LLM_SERVICE_URL', 'http://localhost:5001')

logger = logging.getLogger(__name__)


class RemoteEmbedder:
    def __init__(self, service_url):
        self.service_url = service_url

    def process(self, inputs):
        """
        调用远程 LLM 服务获取 embedding
        """
        try:
            response = requests.post(
                f"{self.service_url}/embed",
                json={'inputs': inputs},
                timeout=300  # 设置较长的超时时间，因为模型推理可能较慢
            )
            response.raise_for_status()
            result = response.json()

            if 'embeddings' in result:
                # 返回 tensor 格式可能不方便，这里直接返回 list
                # 调用方需要注意处理 list
                import torch
                return torch.tensor(result['embeddings'])
            else:
                logger.error(f"Unexpected response from LLM service: {result}")
                return None

        except requests.exceptions.RequestException as e:
            logger.error(f"Error calling LLM service: {e}")
            # 如果是连接错误，可能服务未启动
            return None


# 全局实例
_embedder = None


def get_embedder():
    global _embedder
    if _embedder is None:
        _embedder = RemoteEmbedder(LLM_SERVICE_URL)
    return _embedder
