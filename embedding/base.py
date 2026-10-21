"""Embedding 统一接口"""

from abc import ABC, abstractmethod


class BaseEmbedder(ABC):
    """Embedding 基类，本地/API 实现需继承此类"""

    @abstractmethod
    def embed(self, text: str, *, timeout: float | None = None, max_retries: int | None = None) -> list[float]:
        """单条文本转向量"""
        ...

    @abstractmethod
    def embed_batch(
        self, texts: list[str], *, timeout: float | None = None, max_retries: int | None = None
    ) -> list[list[float]]:
        """批量文本转向量"""
        ...

    @abstractmethod
    def get_model_info(self) -> dict:
        """返回模型信息 (model_name, source, dimension, available)"""
        ...
