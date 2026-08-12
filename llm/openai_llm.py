"""OpenAI 兼容 LLM 实现 — 通用，支持所有兼容 OpenAI API 的厂商"""

from typing import Generator
from openai import OpenAI

from llm.base import BaseLlm

# 各厂商默认配置
PROVIDER_DEFAULTS = {
    "openai": {"base_url": "", "model": "gpt-4o-mini"},
    "qwen":   {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus"},
    "zhipu":  {"base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-flash"},
}


class OpenAiLlm(BaseLlm):
    """通用实现 — 只要兼容 OpenAI API 就能用"""

    def __init__(self, api_key: str, base_url: str = "", model: str = "", provider: str = ""):
        defaults = PROVIDER_DEFAULTS.get(provider, {})
        url = base_url or defaults.get("base_url", "")
        self.model = model or defaults.get("model", "gpt-4o-mini")

        self.client = OpenAI(
            api_key=api_key,
            base_url=url if url else None,
        )

    def chat(self, messages: list[dict], temperature: float = 0.7) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=temperature,
        )
        return response.choices[0].message.content

    def chat_stream(self, messages: list[dict], temperature: float = 0.7) -> Generator[str, None, None]:
        stream = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=temperature,
            stream=True,
        )
        for chunk in stream:
            if chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content