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

    def __init__(
        self, provider: str, api_key: str, base_url: str = "", model: str = "", *,
        timeout: float | None = None, max_retries: int | None = None,
    ):
        # 不做厂商兜底：三项缺任一直接报错（provider 仅用于日志/排障，不再据此猜模型）
        require_config({"API Key": api_key, "API 地址": base_url, "Embedding 模型": model})
        self.model_name = model

        options = {"timeout": _EMBED_TIMEOUT if timeout is None else timeout}
        if max_retries is not None:
            options["max_retries"] = max_retries
        self.client = OpenAI(api_key=api_key, base_url=base_url, **options)

    def _client_for(self, timeout: float | None, max_retries: int | None):
        options = {}
        if timeout is not None:
            options["timeout"] = timeout
        if max_retries is not None:
            options["max_retries"] = max_retries
        return self.client.with_options(**options) if options else self.client

    def embed(
        self, text: str, *, timeout: float | None = None, max_retries: int | None = None,
    ) -> list[float]:
        response = self._client_for(timeout, max_retries).embeddings.create(
            model=self.model_name,
            input=text,
        )
        return response.data[0].embedding

    def embed_batch(
        self, texts: list[str], *, timeout: float | None = None, max_retries: int | None = None,
    ) -> list[list[float]]:
        response = self._client_for(timeout, max_retries).embeddings.create(
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
