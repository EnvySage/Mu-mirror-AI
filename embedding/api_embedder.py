"""API Embedding 实现"""

from openai import OpenAI

from config import CONFIG
from embedding.base import BaseEmbedder

# 各厂商默认 Embedding 模型
PROVIDER_DEFAULTS = {
    "openai": {"model": "text-embedding-3-small", "base_url": ""},
    "zhipu": {"model": "embedding-3", "base_url": "https://open.bigmodel.cn/api/paas/v4"},
    "qwen": {"model": "text-embedding-v3", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
}

# 显式超时（第九轮 #3）：与 LLM 调用一致，避免 SDK 默认 600s 白跑
_EMBED_TIMEOUT = float(CONFIG["llm"]["timeout_seconds"])


class ApiEmbedder(BaseEmbedder):
    """API Embedding — 通过 OpenAI 兼容接口调用"""

    def __init__(self, provider: str, api_key: str, base_url: str = "", model: str = ""):
        defaults = PROVIDER_DEFAULTS.get(provider, {})
        self.model_name = model or defaults.get("model", "text-embedding-3-small")
        url = base_url or defaults.get("base_url", "")

        self.client = OpenAI(
            api_key=api_key,
            base_url=url if url else None,
            timeout=_EMBED_TIMEOUT,
        )

    def embed(self, text: str) -> list[float]:
        response = self.client.embeddings.create(
            model=self.model_name,
            input=text,
        )
        return response.data[0].embedding

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        response = self.client.embeddings.create(
            model=self.model_name,
            input=texts,
        )
        return [item.embedding for item in response.data]

    def get_model_info(self) -> dict:
        return {
            "model_name": self.model_name,
            "source": "api",
            "dimension": -1,  # 需要实际调用才能获取
            "available": True,
        }