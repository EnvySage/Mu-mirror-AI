"""Anthropic 协议实现（支持第三方代理）"""

from typing import Generator

import anthropic
from anthropic import APIConnectionError, APIStatusError, APITimeoutError

from config import CONFIG
from errors import (
    AiServiceError,
    LlmTimeoutError,
    LlmUnavailableError,
    translate_llm_sdk_exception,
)
from llm.base import BaseLlm

# 显式超时（第九轮 #3）：anthropic SDK 默认 600s，客户端早已放弃。
# 同 openai：SDK 对 timeout 会按 max_retries 重试，折算到单 attempt 以逼近总预算。
_LLM_TIMEOUT = float(CONFIG["llm"]["timeout_seconds"])
_LLM_MAX_RETRIES = int(CONFIG["llm"]["max_retries"])
_LLM_ATTEMPT_TIMEOUT = max(1.0, round(_LLM_TIMEOUT / (1 + _LLM_MAX_RETRIES)))


class AnthropicLlm(BaseLlm):
    """Anthropic 协议 LLM（支持非 Claude 模型）"""

    def __init__(self, api_key: str, base_url: str = "", model: str = "claude-sonnet-4-20250514"):
        self.client = anthropic.Anthropic(
            api_key=api_key,
            base_url=base_url if base_url else None,
            timeout=_LLM_ATTEMPT_TIMEOUT,
            max_retries=_LLM_MAX_RETRIES,
        )
        self.model = model

    def chat(self, messages: list[dict], temperature: float = 0.7) -> str:
        system, chat_messages = self._split_system(messages)

        kwargs = dict(
            model=self.model,
            max_tokens=4096,
            messages=chat_messages,
            temperature=temperature,
        )
        if system:
            kwargs["system"] = system

        try:
            response = self.client.messages.create(**kwargs)
        except APITimeoutError as e:
            raise LlmTimeoutError(f"LLM 请求超时（>{_LLM_TIMEOUT:.0f}s）") from e
        except APIConnectionError as e:
            raise LlmUnavailableError("LLM 连接失败（anthropic APIConnectionError）") from e
        except APIStatusError as e:
            raise self._from_status(e) from e
        return response.content[0].text

    def chat_stream(self, messages: list[dict],
                    temperature: float = 0.7) -> Generator[tuple[str, str], None, None]:
        """流式对话：每项 (kind, text)，kind ∈ {"thinking", "content"}。

        基于原始事件流（RawMessageStreamEvent）而非 text_stream——text_stream 只吐正文，
        拿不到 thinking_delta。模型不发思考块（mimo 等走 anthropic 协议但无 thinking）时
        只有 text_delta → 只有 content，行为与旧版一致（零影响）。
        """
        system, chat_messages = self._split_system(messages)

        kwargs = dict(
            model=self.model,
            max_tokens=4096,
            messages=chat_messages,
            temperature=temperature,
        )
        if system:
            kwargs["system"] = system

        try:
            stream_ctx = self.client.messages.stream(**kwargs)
        except APITimeoutError as e:
            raise LlmTimeoutError(f"LLM 请求超时（>{_LLM_TIMEOUT:.0f}s）") from e
        except APIConnectionError as e:
            raise LlmUnavailableError("LLM 连接失败（anthropic APIConnectionError）") from e
        except APIStatusError as e:
            raise self._from_status(e) from e

        try:
            with stream_ctx as stream:
                for event in stream:  # MessageStream 可迭代 → ParsedMessageStreamEvent
                    if event.type != "content_block_delta":
                        continue
                    delta = event.delta
                    delta_type = getattr(delta, "type", "")
                    if delta_type == "thinking_delta":
                        thinking = getattr(delta, "thinking", None)
                        if thinking:
                            yield ("thinking", thinking)
                    elif delta_type == "text_delta":
                        text = getattr(delta, "text", None)
                        if text:
                            yield ("content", text)
        except APITimeoutError as e:
            raise LlmTimeoutError("LLM 流式传输超时") from e
        except APIConnectionError as e:
            raise LlmUnavailableError("LLM 流式传输中断（anthropic APIConnectionError）") from e

    @staticmethod
    def _split_system(messages: list[dict]) -> tuple[str, list[dict]]:
        system = ""
        chat_messages = []
        for msg in messages:
            if msg["role"] == "system":
                system = msg["content"]
            else:
                chat_messages.append(msg)
        return system, chat_messages

    @staticmethod
    def _from_status(exc: APIStatusError) -> AiServiceError:
        """按 HTTP status_code 分流（与 openai 侧一致）"""
        code = exc.status_code
        if code in (401, 403, 404):
            return LlmUnavailableError(f"LLM 请求被拒绝（HTTP {code}：鉴权失败或模型不存在）")
        if code == 429:
            return LlmUnavailableError(f"LLM 限流（HTTP 429），可稍后重试")
        if code >= 500:
            return LlmUnavailableError(f"LLM 服务端错误（HTTP {code}）")
        return translate_llm_sdk_exception(exc)
