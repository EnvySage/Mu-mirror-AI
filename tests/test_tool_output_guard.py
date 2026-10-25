"""Fake calls in answer text must never become actions or leak protocol payloads."""

import pytest

from services.answer_service import stream_answer, stream_agent_answer
from services.tool_output_guard import GraphToolCallTextFilter


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
              else stream_agent_answer([], [], [], llm, allow_protocol_examples=True))
    events = list(stream)
    assert events[-1].kind == "done"
    assert llm.closed
    return "".join(e.content for e in events if e.kind == "delta")


@pytest.mark.parametrize("engine", ["legacy"])
@pytest.mark.parametrize("width", [1, 7, 1000])
def test_screenshot_fake_call_is_replaced_across_arbitrary_chunks(engine, width):
    text = '草稿正文。<tool_call><function=operation_card><parameter=draft>{"title":"不应泄漏"}</parameter></function></tool_call>请查看。'
    result = answer([text[i:i + width] for i in range(0, len(text), width)], engine)
    assert "<tool_call" not in result
    assert "operation_card" not in result
    assert "不应泄漏" not in result
    assert "工具调用未完成" not in result
    assert result.startswith("草稿正文。")
    assert result.endswith("请查看。")


@pytest.mark.parametrize("engine", ["legacy"])
def test_unterminated_call_is_suppressed_without_buffering_entire_payload(engine):
    result = answer(list("你好<tool_call>" + "private" * 10000), engine)
    assert result.startswith("你好")
    assert "private" not in result
    assert "<tool_call" not in result
    assert result == "你好"


@pytest.mark.parametrize("engine", ["legacy", "graph_v2"])
def test_normal_text_and_fenced_protocol_examples_remain_intact(engine):
    text = '1 < 2。\n```xml\n<tool_call>示例</tool_call>\n```\n正常正文'
    assert answer(list(text), engine) == text


@pytest.mark.parametrize("engine", ["legacy"])
def test_bracket_tool_call_placeholder_is_blocked(engine):
    result = answer(list("[工具_call id=call_1234567890]"), engine)
    assert "call_1234567890" not in result
    assert "工具调用未完成" in result


@pytest.mark.parametrize("engine", ["legacy"])
def test_only_fake_call_returns_useful_message_instead_of_empty_answer(engine):
    result = answer(["<tool_call><function=operation_card>bad</function></tool_call>"], engine)
    assert "工具调用未完成" in result


@pytest.mark.parametrize("engine", ["legacy"])
@pytest.mark.parametrize("opening,closing", [
    ("<function_calls>", "</function_calls>"),
    ("<TOOL_CALLS>", "</TOOL_CALLS>"),
    ("<function=operation_card>", "</function>"),
])
def test_other_pseudo_call_envelopes_are_also_blocked(engine, opening, closing):
    result = answer(list("正文" + opening + "internal_payload" + closing + "结束"), engine)
    assert "internal_payload" not in result
    assert "工具调用未完成" not in result
    assert result.startswith("正文") and result.endswith("结束")


@pytest.mark.parametrize("width", [1, 7, 1000])
def test_graph_guard_blocks_attribute_variants_even_inside_fences(width):
    content = '```xml\n<tool_call id="private">secret</tool_call>\n```'
    guard = GraphToolCallTextFilter()
    output = "".join(guard.feed(content[i:i + width]) for i in range(0, len(content), width))
    output += guard.flush()
    assert guard.blocked
    assert output == ""
    assert len(guard.pending) <= 16


def test_graph_guard_does_not_buffer_rejected_payload():
    guard = GraphToolCallTextFilter()
    assert guard.feed('<tool_call id="x">' + 'secret' * 10000) == ""
    assert guard.feed('secret' * 10000) == ""
    assert guard.pending == ""


@pytest.mark.parametrize("content", [
    "1 < 2。正常正文", "<tool_callback>ordinary XML</tool_callback>",
    "```python\nprint('hello')\n```", "~~~text\n正常正文\n~~~",
])
def test_graph_guard_preserves_normal_text_and_unrelated_code(content):
    guard = GraphToolCallTextFilter()
    output = "".join(guard.feed(char) for char in content) + guard.flush()
    assert output == content
    assert not guard.blocked


def test_graph_guard_partial_protocol_at_eof_is_blocked():
    guard = GraphToolCallTextFilter()
    assert guard.feed("<tool_call") == ""
    assert guard.flush() == ""
    assert guard.blocked


@pytest.mark.parametrize("content", [
    '{"status":"ok","count":2}', '{"tool":"pencil","count":2}',
    '普通花括号 {不是 JSON}。', '```json\n{"name":"Alice","age":24}\n```',
    '{"text":"' + 'ordinary' * 1000 + '"}',
])
def test_graph_guard_preserves_ordinary_json_and_braces(content):
    guard = GraphToolCallTextFilter()
    output = []
    for char in content:
        output.append(guard.feed(char))
        assert len(guard.pending) <= 4096
    output.append(guard.flush())
    assert "".join(output) == content
    assert not guard.blocked


def test_graph_guard_blocks_json_envelope_before_large_payload_and_stays_bounded():
    guard = GraphToolCallTextFilter()
    for char in '{"tool":"recall_item","args":{"private":"' + 'secret' * 10000:
        assert guard.feed(char) == ""
        assert len(guard.pending) <= 4096
    assert guard.blocked


@pytest.mark.parametrize("content", [
    '{"args":{"private":"' + 'secret' * 1000 + '"},"tool":"recall_item"}',
    '{"tool":"recall_item",' + ' ' * 5000 + '"args":{"private":1}}',
], ids=["args_first", "tool_gap"])
def test_graph_guard_cannot_release_an_oversized_ambiguous_call_envelope(content):
    guard = GraphToolCallTextFilter()
    output = "".join(guard.feed(char) for char in content) + guard.flush()
    assert output == ""
    assert guard.blocked
