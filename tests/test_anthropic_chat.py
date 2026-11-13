from types import SimpleNamespace

import pytest

from llm.anthropic_llm import AnthropicLlm
from mirror_core.errors import LlmUnavailableError


class FakeMessages:
    def __init__(self, content):
        self.content = content

    def create(self, **kwargs):
        return SimpleNamespace(content=self.content)


class FakeClient:
    def __init__(self, content):
        self.messages = FakeMessages(content)


def make_llm(content):
    llm = object.__new__(AnthropicLlm)
    llm.model = "test-model"
    llm.client = FakeClient(content)
    return llm


def test_chat_extracts_text_after_thinking_blocks_without_returning_reasoning():
    llm = make_llm([
        SimpleNamespace(type="thinking", thinking="internal reasoning"),
        SimpleNamespace(type="text", text='{"items": []}'),
    ])

    assert llm.chat([{"role": "user", "content": "classify"}]) == '{"items": []}'


def test_chat_joins_multiple_text_blocks_in_order():
    llm = make_llm([
        SimpleNamespace(type="text", text="first"),
        SimpleNamespace(type="thinking", thinking="internal reasoning"),
        SimpleNamespace(type="text", text="second"),
    ])

    assert llm.chat([{"role": "user", "content": "answer"}]) == "first\nsecond"


def test_chat_rejects_responses_without_a_text_block():
    llm = make_llm([SimpleNamespace(type="thinking", thinking="internal reasoning")])

    with pytest.raises(LlmUnavailableError, match="没有可用文本块"):
        llm.chat([{"role": "user", "content": "classify"}])


def test_json_task_extracts_text_after_thinking_blocks():
    llm = make_llm([
        SimpleNamespace(type="thinking", thinking="internal reasoning"),
        SimpleNamespace(type="text", text='{"route": "SEMANTIC"}'),
    ])

    assert llm.json_task([{"role": "user", "content": "route"}]) == '{"route": "SEMANTIC"}'


def test_json_task_rejects_responses_without_a_text_block():
    llm = make_llm([SimpleNamespace(type="thinking", thinking="internal reasoning")])

    with pytest.raises(LlmUnavailableError, match="没有可用文本块"):
        llm.json_task([{"role": "user", "content": "route"}])


def test_chat_stream_falls_back_to_non_streaming_for_incomplete_proxy_snapshot(monkeypatch):
    llm = make_llm([
        SimpleNamespace(type="thinking", thinking="internal reasoning"),
        SimpleNamespace(type="text", text="The retrieved note says Lotus Lantern."),
    ])

    def broken_stream(*args, **kwargs):
        if False:
            yield ("content", "")
        raise AttributeError("'NoneType' object has no attribute 'append'")

    monkeypatch.setattr(llm, "_iter_stream", broken_stream)

    assert list(llm.chat_stream([{"role": "user", "content": "answer"}], thinking_budget=0)) == [
        ("content", "The retrieved note says Lotus Lantern.")
    ]


def test_chat_stream_does_not_fallback_after_visible_partial_text(monkeypatch):
    llm = make_llm([SimpleNamespace(type="text", text="must not be used")])

    def partial_stream(*args, **kwargs):
        yield ("content", "partial")
        raise AttributeError("'NoneType' object has no attribute 'append'")

    monkeypatch.setattr(llm, "_iter_stream", partial_stream)
    stream = llm.chat_stream([{"role": "user", "content": "answer"}], thinking_budget=0)
    assert next(stream) == ("content", "partial")
    with pytest.raises(AttributeError, match="NoneType.*append"):
        next(stream)


def test_chat_stream_does_not_fallback_for_unrelated_attribute_error(monkeypatch):
    llm = make_llm([SimpleNamespace(type="text", text="must not be used")])

    def broken_stream(*args, **kwargs):
        if False:
            yield ("content", "")
        raise AttributeError("unrelated adapter bug")

    monkeypatch.setattr(llm, "_iter_stream", broken_stream)

    with pytest.raises(AttributeError, match="unrelated adapter bug"):
        list(llm.chat_stream([{"role": "user", "content": "answer"}], thinking_budget=0))
