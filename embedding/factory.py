"""Embedding 工厂函数 — 根据配置动态创建实例"""

from embedding.base import BaseEmbedder
from embedding.local_embedder import LocalEmbedder
from embedding.api_embedder import ApiEmbedder


def create_embedder(
    source: str = "local",
    local_model: str = "BAAI/bge-m3",
    api_provider: str = "",
    api_key: str = "",
    api_model: str = "",
    base_url: str = "",
) -> BaseEmbedder:
    """
    根据配置创建对应的 Embedding 实例

    Args:
        source: 来源 (local / api)
        local_model: 本地模型名称
        api_provider: API 厂商 (openai / zhipu / qwen)
        api_key: API 密钥
        api_model: API 模型名称
        base_url: 自定义 API 地址（可选）
    """
    if source == "local":
        return LocalEmbedder(model_name=local_model)
    elif source == "api":
        if not api_key:
            raise ValueError("API 模式需要提供 api_key")
        return ApiEmbedder(
            provider=api_provider,
            api_key=api_key,
            model=api_model,
            base_url=base_url,
        )
    else:
        raise ValueError(f"未知的 Embedding 来源: {source}")