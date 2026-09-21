"""Anthropic 协议实现（支持第三方代理）"""

from typing import Generator

import anthropic
import httpx  # anthropic SDK 自身依赖，非新增依赖
from anthropic import APIConnectionError, APIStatusError, APITimeoutError

from config import CONFIG
from errors import (
    AiServiceError,
    LlmTimeoutError,
    LlmUnavailableError,
    require_config,
    translate_llm_sdk_exception,
)
from llm.base import BaseLlm

# 显式超时（第九轮 #3）：anthropic SDK 默认 600s，客户端早已放弃。
# 同 openai：SDK 对 timeout 会按 max_retries 重试，折算到单 attempt 以逼近总预算。
_LLM_TIMEOUT = float(CONFIG["llm"]["timeout_seconds"])
_LLM_MAX_RETRIES = int(CONFIG["llm"]["max_retries"])
_LLM_ATTEMPT_TIMEOUT = max(1.0, round(_LLM_TIMEOUT / (1 + _LLM_MAX_RETRIES)))

# 流式调用单独的超时：read = "两块数据之间最多等多久"。不能沿用上面的单 attempt 超时——
# 2026-09-21 联调实测 mimo 思考中途/思考转正文时会停顿十几秒，按 10s 读超时直接断流，
# 已算好的答案全部作废、用户看到"暂时无法回答"。connect 仍保持短，死端点照样快速失败。
_STREAM_READ_TIMEOUT = float(CONFIG["llm"].get("stream_read_timeout_seconds", 60))
_STREAM_TIMEOUT = httpx.Timeout(_STREAM_READ_TIMEOUT, connect=10.0)

# 轻量 JSON 任务的 max_tokens 下限：关思考后实测只吐 40~50 token，256 足够且留足余量。
# 注意不能压到 300 以下又不关思考——思考吃满预算会把 JSON 整个截断（实测裸奔）。
_JSON_TASK_MAX_TOKENS = 256

# 端点不支持 thinking 参数（400/404）时降级重试一次，避免把老模型/老代理打死
_THINKING_UNSUPPORTED_HINTS = ("thinking", "unexpected keyword", "unknown parameter", "not supported")

# 最终回答（chat_stream）的 max_tokens
_CHAT_MAX_TOKENS = 4096

# extended thinking 预算（config.yml llm.thinking_budget_tokens；0 = 关闭）。
# Anthropic 的思考块不是默认行为：不显式传 thinking={"type":"enabled",...}
# 就永远收不到 thinking_delta → B 侧 ChatChunk.thinking 恒空 → 前端思考面板不出现。
# json_task（意图路由/工具规划）走 disabled，不受此配置影响。
_THINKING_BUDGET = int(CONFIG["llm"].get("thinking_budget_tokens", 0))


class AnthropicLlm(BaseLlm):
    """Anthropic 协议 LLM（支持非 Claude 模型）"""

    def __init__(self, api_key: str, base_url: str = "", model: str = ""):
        # 不兜底默认模型（原为写死的 claude-sonnet-4-20250514）：没配就报错，
        # 否则"未配置"会被静默替换成另一个模型真实计费
        require_config({"API Key": api_key, "模型名称": model})
        self.model = model
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
                    temperature: float = 0.7,
                    thinking_budget: int | None = None) -> Generator[tuple[str, str], None, None]:
        """流式对话：每项 (kind, text)，kind ∈ {"thinking", "content"}。

        基于原始事件流（RawMessageStreamEvent）而非 text_stream——text_stream 只吐正文，
        拿不到 thinking_delta。

        Anthropic 的 extended thinking 必须**显式开启**（thinking={"type":"enabled",
        "budget_tokens":N}），不传该参数时 thinking_delta 永不出现——这正是"思考过程
        以前有、现在没有"的根因。端点/模型不认该参数（400）时降级为普通流重试一次，
        功能不受影响、只是没有思考面板（mimo 等无 thinking 的模型同样只有 text_delta）。
        """
        system, chat_messages = self._split_system(messages)

        base = dict(
            model=self.model,
            max_tokens=_CHAT_MAX_TOKENS,
            messages=chat_messages,
        )
        if system:
            base["system"] = system

        # 调用方可覆盖预算（规划器用小预算，见 plan_service.PlanNextStep）；None = 全局配置
        budget = _THINKING_BUDGET if thinking_budget is None else int(thinking_budget)
        if budget > 0:
            thinking_kwargs = {
                **base,
                # anthropic 要求 max_tokens > budget_tokens（否则 400），给正文留足余量
                "max_tokens": max(_CHAT_MAX_TOKENS, budget + 1024),
                "thinking": {"type": "enabled", "budget_tokens": budget},
                # 开启 thinking 时 anthropic 只接受 temperature=1，故此处不传该参数
            }
            try:
                yielded = False
                for item in self._iter_stream(thinking_kwargs):
                    yielded = True
                    yield item
                return
            except APIStatusError as e:
                # 只对"参数不被支持"这一种 400 降级，且必须尚未产出任何块
                # （已吐过内容再降级重来会重复输出）
                if yielded or not self._thinking_unsupported(e):
                    raise self._from_status(e) from e

        try:
            yield from self._iter_stream({**base, "temperature": temperature})
        except APIStatusError as e:
            # 普通路径也要按状态码翻译（401/429/5xx → AiServiceError），与旧行为一致
            raise self._from_status(e) from e

    def _iter_stream(self, kwargs: dict) -> Generator[tuple[str, str], None, None]:
        """单次流式调用的事件翻译。

        参数/鉴权类错误（APIStatusError）在首个事件前原样抛出、不翻译——
        上层据此决定是否降级重试；超时/断连在此翻译为 AiServiceError。
        """
        try:
            stream_ctx = self.client.messages.stream(**kwargs, timeout=_STREAM_TIMEOUT)
        except APITimeoutError as e:
            raise LlmTimeoutError(f"LLM 流式连接超时（>{_STREAM_READ_TIMEOUT:.0f}s）") from e
        except APIConnectionError as e:
            raise LlmUnavailableError("LLM 连接失败（anthropic APIConnectionError）") from e

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
            raise LlmTimeoutError(f"LLM 流式传输超时（两块数据间隔 >{_STREAM_READ_TIMEOUT:.0f}s）") from e
        except APIConnectionError as e:
            raise LlmUnavailableError("LLM 流式传输中断（anthropic APIConnectionError）") from e

    @staticmethod
    def _thinking_unsupported(exc: APIStatusError) -> bool:
        """400 且报错文案指向 thinking 参数不被支持（其余 400 照常报错，防掩盖真错误）"""
        return exc.status_code == 400 and any(
            h in str(getattr(exc, "message", "") or exc).lower()
            for h in _THINKING_UNSUPPORTED_HINTS)

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

    def json_task(self, messages: list[dict], temperature: float = 0.7) -> str:
        """轻量 JSON 任务：关思考 + 小 max_tokens（实测 22.6s → 2.1s，输出 1074 → 43 token）

        thinking={"type": "disabled"} 是 anthropic 协议标准写法，mimo 等兼容端点同样接受。
        端点不认该参数时（400）降级为普通调用重试一次：功能不受影响，只是慢回原样。
        """
        system, chat_messages = self._split_system(messages)
        base = dict(
            model=self.model,
            max_tokens=_JSON_TASK_MAX_TOKENS,
            messages=chat_messages,
            temperature=temperature,
        )
        if system:
            base["system"] = system

        try:
            return self._create_text({**base, "thinking": {"type": "disabled"}})
        except APIStatusError as e:
            # 只对"参数不被支持"这一种 400 降级；其余 400（如请求本身非法）照常报错
            if self._thinking_unsupported(e):
                return self._create_text(base)
            raise self._from_status(e) from e

    def _create_text(self, kwargs: dict) -> str:
        """messages.create 的异常翻译 + 取正文（chat / json_task 共用）

        APIStatusError 原样上抛（不翻译）：json_task 需要按 status_code 决定是否降级。
        """
        try:
            response = self.client.messages.create(**kwargs)
        except APITimeoutError as e:
            raise LlmTimeoutError(f"LLM 请求超时（>{_LLM_TIMEOUT:.0f}s）") from e
        except APIConnectionError as e:
            raise LlmUnavailableError("LLM 连接失败（anthropic APIConnectionError）") from e
        return response.content[0].text
