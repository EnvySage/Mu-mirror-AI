"""API Embedding 实现"""

from openai import OpenAI

from embedding.base import BaseEmbedder

# 各厂商默认 Embedding 模型
PROVIDER_DEFAULTS = {
    "openai": {"model": "text-embedding-3-small", "base_url": ""},
    "zhipu": {"model": "embedding-3", "base_url": "https://open.bigmodel.cn/api/paas/v4"},
    "qwen": {"model": "text-embedding-v3", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
}


class ApiEmbedder(BaseEmbedder):
    """API Embedding — 通过 OpenAI 兼容接口调用"""

    def __init__(self, provider: str, api_key: str, base_url: str = "", model: str = ""):
        defaults = PROVIDER_DEFAULTS.get(provider, {})
        self.model_name = model or defaults.get("model", "text-embedding-3-small")
        url = base_url or defaults.get("base_url", "")

        self.client = OpenAI(
            api_key=api_key,
            base_url=url if url else None,
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