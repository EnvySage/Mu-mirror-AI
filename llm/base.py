"""LLM 统一接口"""

from abc import ABC, abstractmethod
from typing import Generator


class BaseLlm(ABC):
    """LLM 基类，所有厂商实现需继承此类"""

    @abstractmethod
    def chat(
        self, messages: list[dict], temperature: float = 0.7, *,
        timeout: float | None = None, max_retries: int | None = None,
    ) -> str:
        """同步对话，返回完整回复"""
        ...

    @abstractmethod
    def chat_stream(self, messages: list[dict],
                    temperature: float = 0.7,
                    thinking_budget: int | None = None, *,
                    timeout: float | None = None,
                    max_retries: int | None = None,
                    max_tokens: int | None = None) -> Generator[tuple[str, str], None, None]:
        """流式对话，逐块返回 (kind, text)：kind ∈ {"thinking", "content"}

        thinking_budget：本次调用的思考预算（token）；None = 用全局 llm.thinking_budget_tokens。
        规划器要单独传一个小预算——见 plan_service.PlanNextStep 的说明。
        max_tokens 可为含记录正文的工具计划单独设置输出上限；None 保留厂商默认。
        """
        ...

    def json_task(
        self, messages: list[dict], temperature: float = 0.7, *,
        timeout: float | None = None, max_retries: int | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """轻量 JSON 任务（意图路由 / 工具规划）：**关闭思考**，只要结论。

        推理型模型（mimo-v2.5 等）默认开思考，一个四选一的路由任务会生成
        1000+ 思考 token 再吐 40 个 token 的 JSON——实测 22.6s vs 关思考后 2.1s（10x）。
        这些前置调用在"用户看到第一个字之前"串行执行，是全链路感知延迟的主因，
        且它们的产出是结构化短 JSON，不需要思维链。

        默认实现退回 chat()（不支持的厂商零影响）；能关思考的实现在子类覆写。
        """
        options = {}
        if timeout is not None:
            options["timeout"] = timeout
        if max_retries is not None:
            options["max_retries"] = max_retries
        return self.chat(messages, temperature=temperature, **options)

    def plan(
        self, messages: list[dict], tools: list[dict], *, protocol: str = "auto",
        timeout: float | None = None, max_retries: int = 0,
    ) -> dict:
        """Graph-only text planning default for adapters without native tools."""
        from llm.planning import PLAN_MAX_TOKENS, PlanningProtocolUnsupported, planning_messages, text_plan
        if protocol not in ("auto", "text_json"):
            raise PlanningProtocolUnsupported(f"Planning protocol {protocol} is not supported by this adapter")
        text = "".join(part for kind, part in self.chat_stream(
            planning_messages(messages, tools), thinking_budget=0, timeout=timeout,
            max_retries=max_retries, max_tokens=PLAN_MAX_TOKENS,
        ) if kind == "content")
        return text_plan(text, "text_json", None)

    def answer_stream(
        self, messages: list[dict], temperature: float = 0.7,
        thinking_budget: int | None = None, *, timeout: float | None = None,
        max_retries: int | None = None, max_tokens: int | None = None,
    ) -> Generator[tuple[str, str], None, None]:
        """Graph-only answer stream; SDK adapters expose the actual finish reason."""
        yield from self.chat_stream(
            messages, temperature, thinking_budget, timeout=timeout,
            max_retries=max_retries, max_tokens=max_tokens,
        )
        yield ("finish", "unknown")
