"""A planner containing record text can request room without changing answer defaults."""

from types import SimpleNamespace

import pytest

from llm.openai_llm import OpenAiLlm
from llm.anthropic_llm import AnthropicLlm


def test_openai_stream_forwards_explicit_output_budget():
    captured = {}
    def create(**kwargs):
        captured.update(kwargs)
        return iter(())
    llm = object.__new__(OpenAiLlm)
    llm.model = "test"
    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    list(llm.chat_stream([], max_tokens=8192))
    assert captured["max_tokens"] == 8192


@pytest.mark.parametrize("thinking_budget", [0, 2048, 10000])
def test_anthropic_stream_keeps_record_budget_with_or_without_thinking(thinking_budget):
    captured = {}
    llm = object.__new__(AnthropicLlm)
    llm.model = "test"
    def stream(kwargs, **options):
        captured.update(kwargs)
        return iter(())
    llm._iter_stream = stream
    list(llm.chat_stream([], max_tokens=8192, thinking_budget=thinking_budget))
    assert captured["max_tokens"] >= 8192
    assert captured["max_tokens"] > thinking_budget
