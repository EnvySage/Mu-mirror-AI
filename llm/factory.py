"""LLM 工厂函数"""

from llm.base import BaseLlm
from llm.openai_llm import OpenAiLlm
from llm.anthropic_llm import AnthropicLlm


def create_llm(provider: str, api_key: str, base_url: str = "", model: str = "", ai_protocol: str = "") -> BaseLlm:
    """
    创建 LLM 实例

    Args:
        provider: 厂商标识
        api_key: API 密钥
        base_url: 自定义 API 地址
        model: 模型名称
        ai_protocol: 协议 (openai / anthropic)，默认 openai
    """
    protocol = ai_protocol or "openai"

    if protocol == "anthropic":
        return AnthropicLlm(api_key=api_key, model=model or "claude-sonnet-4-20250514")

    # 默认 OpenAI 兼容
    return OpenAiLlm(api_key=api_key, base_url=base_url, model=model, provider=provider)