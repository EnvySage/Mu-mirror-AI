"""OpenAI 兼容 LLM 实现 — 通用，支持所有兼容 OpenAI API 的厂商"""

from typing import Generator

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI

from config import CONFIG
from errors import (
    AiServiceError,
    LlmTimeoutError,
    LlmUnavailableError,
    translate_llm_sdk_exception,
)
from llm.base import BaseLlm

# 各厂商默认配置
PROVIDER_DEFAULTS = {
    "openai": {"base_url": "", "model": "gpt-4o-mini"},
    "qwen":   {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus"},
    "zhipu":  {"base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-flash"},
}

# 显式超时（第九轮 #3）：openai SDK 默认 600s，Java 客户端 15s 就放弃，Python 白跑 585s。
# config.yml llm.timeout_seconds 可配置，默认 20s。
# 注意：openai SDK 对 timeout 会重试（max_retries 次 × timeout），总耗时 ≈ (1+retries)*timeout；
# 任务书要求 20s 级别返回，因此把 timeout 拆到 attempts 层面等价控制——SDK 不提供
# total_deadline 参数，这里用 max_retries=1 + timeout 缩小为 timeout/(1+retries) 折算，
# 单 attempt timeout = ceil(timeout / (1 + max_retries))，最坏总耗时逼近 timeout_seconds。
_LLM_TIMEOUT = float(CONFIG["llm"]["timeout_seconds"])
_LLM_MAX_RETRIES = int(CONFIG["llm"]["max_retries"])
_LLM_ATTEMPT_TIMEOUT = max(1.0, round(_LLM_TIMEOUT / (1 + _LLM_MAX_RETRIES)))

# 轻量 JSON 任务的 max_tokens（关思考后实测只吐 40~50 token，256 足够且留足余量）
_JSON_TASK_MAX_TOKENS = 256

# 端点不认识 thinking 参数时的报错特征：只有命中才降级（其余 400 照常报错）
_THINKING_UNSUPPORTED_HINTS = ("thinking", "unexpected keyword", "unknown parameter", "not supported")


def _translate(exc: Exception) -> AiServiceError:
    """SDK 异常 → AiServiceError（本文件保留直接 import 以便类型/测试引用）"""
    return translate_llm_sdk_exception(exc)


class OpenAiLlm(BaseLlm):
    """通用实现 — 只要兼容 OpenAI API 就能用"""

    def __init__(self, api_key: str, base_url: str = "", model: str = "", provider: str = ""):
        defaults = PROVIDER_DEFAULTS.get(provider, {})
        url = base_url or defaults.get("base_url", "")
        self.model = model or defaults.get("model", "gpt-4o-mini")

        self.client = OpenAI(
            api_key=api_key,
            base_url=url if url else None,
            timeout=_LLM_ATTEMPT_TIMEOUT,
            max_retries=_LLM_MAX_RETRIES,
        )

    def chat(self, messages: list[dict], temperature: float = 0.7) -> str:
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
            )
        except APITimeoutError as e:
            raise LlmTimeoutError(f"LLM 请求超时（>{_LLM_TIMEOUT:.0f}s）") from e
        except APIConnectionError as e:  # 连接失败/断开（超时已被上一行截获）
            raise LlmUnavailableError("LLM 连接失败（openai APIConnectionError）") from e
        except APIStatusError as e:
            raise self._from_status(e) from e
        return response.choices[0].message.content

    def chat_stream(self, messages: list[dict],
                    temperature: float = 0.7) -> Generator[tuple[str, str], None, None]:
        """流式对话：每项 (kind, text)，kind ∈ {"thinking", "content"}。

        reasoning_content 是 DeepSeek/mimo 等 OpenAI 兼容系的扩展增量字段（SDK 的
        ChoiceDelta 没有该属性声明，靠 model_extra 承载）——必须 getattr 防御，
        SDK 未带该属性的 chunk 直接得 None，零影响。
        """
        try:
            stream = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
                stream=True,
            )
        except APITimeoutError as e:
            raise LlmTimeoutError(f"LLM 请求超时（>{_LLM_TIMEOUT:.0f}s）") from e
        except APIConnectionError as e:
            raise LlmUnavailableError("LLM 连接失败（openai APIConnectionError）") from e
        except APIStatusError as e:
            raise self._from_status(e) from e
        try:
            for chunk in stream:
                choices = chunk.choices or []
                delta = choices[0].delta if choices else None
                if delta is None:
                    continue
                reasoning = getattr(delta, "reasoning_content", None)
                if reasoning:
                    yield ("thinking", reasoning)
                if delta.content:
                    yield ("content", delta.content)
        except APIConnectionError as e:
            # 流中途断连（网络抖动/网关超时）
            raise LlmUnavailableError("LLM 流式传输中断（openai APIConnectionError）") from e

    @staticmethod
    def _from_status(exc: APIStatusError) -> AiServiceError:
        """按 HTTP status_code 分流（任务书 #3 指定映射）"""
        code = exc.status_code
        if code in (401, 403, 404):
            return LlmUnavailableError(f"LLM 请求被拒绝（HTTP {code}：鉴权失败或模型不存在）")
        if code == 429:
            return LlmUnavailableError(f"LLM 限流（HTTP 429），可稍后重试")
        if code >= 500:
            return LlmUnavailableError(f"LLM 服务端错误（HTTP {code}）")
        return translate_llm_sdk_exception(exc)  # 其他 4xx 走统一规则

    def json_task(self, messages: list[dict], temperature: float = 0.7) -> str:
        """轻量 JSON 任务：关思考 + 小 max_tokens（意图路由 / 工具规划）

        OpenAI 兼容系的思考开关没有统一字段名，这里按常见的 thinking 走 extra_body；
        端点不认该参数时（400）降级为普通调用重试一次：功能不受影响，只是慢回原样。
        """
        base = dict(
            model=self.model,
            messages=messages,
            temperature=temperature,
            max_tokens=_JSON_TASK_MAX_TOKENS,
        )
        try:
            return self._create_text({**base, "extra_body": {"thinking": {"type": "disabled"}}})
        except APIStatusError as e:
            # 只对"参数不被支持"这一种 400 降级；其余 400（如请求本身非法）照常报错
            if e.status_code == 400 and any(
                    h in str(getattr(e, "message", "") or e).lower()
                    for h in _THINKING_UNSUPPORTED_HINTS):
                return self._create_text(base)
            raise self._from_status(e) from e

    def _create_text(self, kwargs: dict) -> str:
        """chat.completions.create 的异常翻译 + 取正文（chat / json_task 共用）

        APIStatusError 原样上抛（不翻译）：json_task 需要按 status_code 决定是否降级。
        """
        try:
            response = self.client.chat.completions.create(**kwargs)
        except APITimeoutError as e:
            raise LlmTimeoutError(f"LLM 请求超时（>{_LLM_TIMEOUT:.0f}s）") from e
        except APIConnectionError as e:
            raise LlmUnavailableError("LLM 连接失败（openai APIConnectionError）") from e
        return response.choices[0].message.content
