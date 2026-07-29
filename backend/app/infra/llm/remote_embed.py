"""远程 embedding 服务客户端（独立 LLM 进程）；连接失败返回 None 由调用方降级。"""

import os

import httpx
from loguru import logger

LLM_SERVICE_URL = os.environ.get('LLM_SERVICE_URL', 'http://localhost:5001')

class RemoteEmbedder:
    def __init__(self, service_url):
        self.service_url = service_url

    async def process(self, inputs):
        """调用远程 /embed；超时偏长以覆盖慢推理，失败返回 None。"""
        try:
            async with httpx.AsyncClient(timeout=300) as client:
                response = await client.post(
                    f"{self.service_url}/embed",
                    json={'inputs': inputs},
                )
            response.raise_for_status()
            result = response.json()

            if 'embeddings' in result:
                # 远端返回 list，包装为 tensor 以兼容历史调用方
                import torch
                return torch.tensor(result['embeddings'])
            else:
                logger.error("LLM 服务响应格式异常: {}", result)
                return None

        except httpx.HTTPError as e:
            logger.error("调用 LLM 服务失败: {}", e)
            return None


_embedder = None


def get_embedder():
    """懒加载全局 RemoteEmbedder 单例。"""
    global _embedder
    if _embedder is None:
        _embedder = RemoteEmbedder(LLM_SERVICE_URL)
    return _embedder
