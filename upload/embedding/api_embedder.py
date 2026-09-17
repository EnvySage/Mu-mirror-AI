"""API Embedding 实现"""

from openai import OpenAI

from config import CONFIG
from embedding.base import BaseEmbedder
from errors import require_config

# 不再维护 PROVIDER_DEFAULTS：早期版本按 provider 兜底 embedding 模型/地址
# （qwen→text-embedding-v3 等），"用户没配"会被静默替换成别的模型真实调用计费。
# 契约：api_key / base_url / model 必须显式给全，缺即报错。

# 显式超时（第九轮 #3）：与 LLM 调用一致，避免 SDK 默认 600s 白跑
_EMBED_TIMEOUT = float(CONFIG["llm"]["timeout_seconds"])


class ApiEmbedder(BaseEmbedder):
    """API Embedding — 通过 OpenAI 兼容接口调用"""

    def __init__(self, provider: str, api_key: str, base_url: str = "", model: str = ""):
        # 不做厂商兜底：三项缺任一直接报错（provider 仅用于日志/排障，不再据此猜模型）
        require_config({"API Key": api_key, "API 地址": base_url, "Embedding 模型": model})
        self.model_name = model

        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url,
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