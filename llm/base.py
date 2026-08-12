"""LLM 统一接口"""

from abc import ABC, abstractmethod
from typing import Generator


class BaseLlm(ABC):
    """LLM 基类，所有厂商实现需继承此类"""

    @abstractmethod
    def chat(self, messages: list[dict], temperature: float = 0.7) -> str:
        """同步对话，返回完整回复"""
        ...

    @abstractmethod
    def chat_stream(self, messages: list[dict], temperature: float = 0.7) -> Generator[str, None, None]:
        """流式对话，逐块返回回复"""
        ...