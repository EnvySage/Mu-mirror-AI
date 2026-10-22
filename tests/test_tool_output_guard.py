"""Fake calls in answer text must never become actions or leak protocol payloads."""

import pytest

from services.answer_service import stream_answer, stream_agent_answer


class ScriptedAnswer:
    def __init__(self, parts):
        self.parts = parts
        self.closed = False

    def chat_stream(self, messages, **kwargs):
        try:
            for part in self.parts:
                yield "content", part
        finally:
            self.closed = True


def answer(parts, engine):
    llm = ScriptedAnswer(parts)
    stream = (stream_answer([], [], [], llm) if engine == "legacy"
              else stream_agent_answer([], [], [], llm))
    events = list(stream)
    assert events[-1].kind == "done"
    assert llm.closed
    return "".join(e.content for e in events if e.kind == "delta")


@pytest.mark.parametrize("engine", ["legacy", "graph_v2"])
@pytest.mark.parametrize("width", [1, 7, 1000])
def test_screenshot_fake_call_is_replaced_across_arbitrary_chunks(engine, width):
    text = '草稿正文。<tool_call><function=operation_card><parameter=draft>{"title":"不应泄漏"}</parameter></function></tool_call>请查看。'
    result = answer([text[i:i + width] for i in range(0, len(text), width)], engine)
    assert "<tool_call" not in result
    assert "operation_card" not in result
    assert "不应泄漏" not in result
    assert "工具调用未完成" in result
    assert result.startswith("草稿正文。")
    assert result.endswith("请查看。")


@pytest.mark.parametrize("engine", ["legacy", "graph_v2"])
def test_unterminated_call_is_suppressed_without_buffering_entire_payload(engine):
    result = answer(list("你好<tool_call>" + "private" * 10000), engine)
    assert result.startswith("你好")
    assert "private" not in result
    assert "<tool_call" not in result
    assert result.count("工具调用未完成") == 1


@pytest.mark.parametrize("engine", ["legacy", "graph_v2"])
def test_normal_text_and_fenced_protocol_examples_remain_intact(engine):
    text = '1 < 2。\n```xml\n<tool_call>示例</tool_call>\n```\n正常正文'
    assert answer(list(text), engine) == text


@pytest.mark.parametrize("engine", ["legacy", "graph_v2"])
def test_only_fake_call_returns_useful_message_instead_of_empty_answer(engine):
    result = answer(["<tool_call><function=operation_card>bad</function></tool_call>"], engine)
    assert "工具调用未完成" in result


@pytest.mark.parametrize("engine", ["legacy", "graph_v2"])
@pytest.mark.parametrize("opening,closing", [
    ("<function_calls>", "</function_calls>"),
    ("<TOOL_CALLS>", "</TOOL_CALLS>"),
    ("<function=operation_card>", "</function>"),
])
def test_other_pseudo_call_envelopes_are_also_blocked(engine, opening, closing):
    result = answer(list("正文" + opening + "internal_payload" + closing + "结束"), engine)
    assert "internal_payload" not in result
    assert "工具调用未完成" in result
    assert result.startswith("正文") and result.endswith("结束")
