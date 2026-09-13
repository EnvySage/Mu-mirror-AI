"""LLM 工厂函数"""

from llm.base import BaseLlm
from llm.openai_llm import OpenAiLlm
from llm.anthropic_llm import AnthropicLlm

from generated import common_pb2 as common


def create_llm(provider: str, api_key: str, base_url: str = "", model: str = "", protocol: int = 0) -> BaseLlm:
    """
    创建 LLM 实例

    Args:
        provider: 厂商标识
        api_key: API 密钥
        base_url: 自定义 API 地址
        model: 模型名称
        protocol: AiProtocol 枚举值 (0=UNKNOWN, 1=OPENAI, 2=ANTHROPIC)
    """
    if protocol == common.AiProtocol.ANTHROPIC:
        return AnthropicLlm(api_key=api_key, base_url=base_url, model=model or "claude-sonnet-4-20250514")

    # 默认 OpenAI 兼容
    return OpenAiLlm(api_key=api_key, base_url=base_url, model=model, provider=provider)