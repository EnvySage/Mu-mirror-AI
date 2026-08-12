"""Anthropic Claude 实现"""

from typing import Generator
import anthropic

from llm.base import BaseLlm


class AnthropicLlm(BaseLlm):
    """Anthropic Claude"""

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-20250514"):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    def chat(self, messages: list[dict], temperature: float = 0.7) -> str:
        # 提取 system message
        system = ""
        chat_messages = []
        for msg in messages:
            if msg["role"] == "system":
                system = msg["content"]
            else:
                chat_messages.append(msg)

        kwargs = dict(
            model=self.model,
            max_tokens=4096,
            messages=chat_messages,
            temperature=temperature,
        )
        if system:
            kwargs["system"] = system

        response = self.client.messages.create(**kwargs)
        return response.content[0].text

    def chat_stream(self, messages: list[dict], temperature: float = 0.7) -> Generator[str, None, None]:
        system = ""
        chat_messages = []
        for msg in messages:
            if msg["role"] == "system":
                system = msg["content"]
            else:
                chat_messages.append(msg)

        kwargs = dict(
            model=self.model,
            max_tokens=4096,
            messages=chat_messages,
            temperature=temperature,
        )
        if system:
            kwargs["system"] = system

        with self.client.messages.stream(**kwargs) as stream:
            for text in stream.text_stream:
                yield text