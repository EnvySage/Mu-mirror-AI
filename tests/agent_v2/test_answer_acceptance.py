"""A graph answer succeeds only after one uninterrupted, accepted generation."""

import pytest


@pytest.mark.parametrize("text", [
    '{"calls":[{"tool":"recall_item","args":{"vault_item_id":31}}],"done":true}',
    '{"function":{"name":"recall_item","arguments":"{\\"vault_item_id\\":31}"}}',
    '<tool_use name="recall_item">payload</tool_use>',
    '<invoke name="recall_item">payload</invoke>',
])
@pytest.mark.parametrize("width", [1, 7, 1000])
def test_graph_planning_json_envelope_cannot_be_published_as_an_answer(text, width):
    from services.answer_service import stream_agent_answer
    class PlanInsteadOfAnswer:
        def chat_stream(self, *args, **kwargs):
            for offset in range(0, len(text), width):
                yield "content", text[offset:offset + width]
    events = list(stream_agent_answer([], [], [], PlanInsteadOfAnswer()))
    assert not any(event.kind == "done" for event in events)
    assert not any(event.kind == "delta" for event in events)
    assert events[-1].metadata["code"] == "OUTPUT_PROTOCOL_BLOCKED"

from agent_core.types import Evidence
from services.answer_service import stream_agent_answer


class ScriptedAnswer:
    def __init__(self, *attempts):
        self.attempts = list(attempts)
        self.calls = []
        self.closed = 0

    def answer_stream(self, messages, *, timeout=None, max_retries=None):
        attempt = self.attempts[len(self.calls)]
        self.calls.append((messages, timeout, max_retries))
        try:
            yield from attempt
        finally:
            self.closed += 1

    def chat_stream(self, *args, **kwargs):
        yield from self.answer_stream(*args, **kwargs)


def text(events):
    return "".join(event.content for event in events if event.kind == "delta")


@pytest.mark.parametrize("protocol", [
    '<tool_call id="x">secret</tool_call>',
    '<TOOL_CALLS name="x">secret</TOOL_CALLS>',
    '<function_calls id="x">secret</function_calls>',
    '<function=operation_card>secret</function>',
    '[工具_call id=secret]',
    '```xml\n<tool_call id="x">secret</tool_call>\n```',
    '~~~xml\n<tool_call>secret</tool_call>\n~~~',
])
@pytest.mark.parametrize("width", [1, 9, 1000])
def test_protocol_before_visible_answer_repairs_once(protocol, width):
    llm = ScriptedAnswer(
        [("text", protocol[i:i + width]) for i in range(0, len(protocol), width)],
        [("text", "正确答案"), ("finish", "stop")],
    )
    events = list(stream_agent_answer([], [], [], llm))
    assert text(events) == "正确答案"
    assert events[-1].kind == "done"
    assert events[-1].metadata == {"finish_reason": "stop", "repair_count": 1}
    assert len(llm.calls) == llm.closed == 2


@pytest.mark.parametrize("same_chunk", [False, True])
def test_published_prefix_never_gets_a_different_generation_appended(same_chunk):
    parts = ["结论甲。", '<tool_call id="x">private</tool_call>尾巴']
    if same_chunk:
        parts = ["".join(parts)]
    llm = ScriptedAnswer([("text", part) for part in parts], [("text", "结论乙。")])
    events = list(stream_agent_answer([], [], [], llm))
    assert text(events) in ("", "结论甲。")
    assert events[-1].kind == "failed"
    assert events[-1].metadata["code"] == "OUTPUT_PROTOCOL_BLOCKED"
    assert len(llm.calls) == llm.closed == 1


def test_repeated_protocol_fails_after_one_repair_without_publishing_it():
    llm = ScriptedAnswer(*([[("text", "<tool_call>secret</tool_call>")]] * 2))
    events = list(stream_agent_answer([], [], [], llm))
    assert text(events) == ""
    assert events[-1].kind == "failed"
    assert events[-1].metadata["code"] == "OUTPUT_PROTOCOL_BLOCKED"
    assert len(llm.calls) == 2


@pytest.mark.parametrize("question,allow", [
    ("请解释 tool_call 协议并给出 XML 示例", None),
    ("Show an example of the tool_call protocol", None),
    ("普通问题", True),
])
def test_explicit_protocol_examples_are_preserved(question, allow):
    example = '```xml\n<tool_call id="x">示例</tool_call>\n```'
    llm = ScriptedAnswer([("text", char) for char in example] + [("finish", "stop")])
    events = list(stream_agent_answer(
        [{"role": "user", "content": question}], [], [], llm,
        allow_protocol_examples=allow,
    ))
    assert text(events) == example
    assert events[-1].kind == "done"


def test_history_does_not_authorize_protocol_examples_for_current_question():
    llm = ScriptedAnswer(*([[("text", "<tool_call>secret</tool_call>")]] * 2))
    events = list(stream_agent_answer([
        {"role": "user", "content": "Show a tool_call protocol example"},
        {"role": "user", "content": "分析我的资料"},
    ], [], [], llm))
    assert text(events) == ""
    assert events[-1].kind == "failed"


@pytest.mark.parametrize("parts,reason,code", [
    ([], "stop", "OUTPUT_EMPTY"),
    ([" \n", "\t"], "stop", "OUTPUT_EMPTY"),
    (["未完成的分析"], "length", "OUTPUT_TRUNCATED"),
    (["未完成的分析"], "max_tokens", "OUTPUT_TRUNCATED"),
])
def test_empty_and_truncated_answers_cannot_succeed(parts, reason, code):
    llm = ScriptedAnswer([("text", part) for part in parts] + [("finish", reason)])
    events = list(stream_agent_answer([], [], [], llm))
    assert events[-1].kind == "failed"
    assert events[-1].metadata["code"] == code
    assert reason not in text(events)
    assert len(llm.calls) == 1


def test_normal_answer_streams_before_completion_and_keeps_authorized_citations():
    evidence = [Evidence("c1", "diary", record_id=12, chunk_id=1, text="fact"),
                Evidence("v1", "vault", vault_item_id=4, text="file")]
    llm = ScriptedAnswer([("thinking", "推理"), ("text", "支持[1]"),
                          ("text", "文件[F1] 假[F9]"), ("finish", "stop")])
    stream = stream_agent_answer([], evidence, [], llm)
    assert next(stream).kind == "thinking"
    assert next(stream).content == "支持[1]"
    assert llm.closed == 0
    events = list(stream)
    assert text(events) == "文件[F1] 假"
    assert events[-1].kind == "done"
    assert events[-1].sources[0].record_id == 12
    assert events[-1].vault_refs[0].vault_item_id == 4
    assert events[-1].metadata["finish_reason"] == "stop"


def test_cancelled_output_closes_stream_without_repair_or_success():
    cancelled = [False]
    llm = ScriptedAnswer([("text", "第一段"), ("text", "不可见")])
    stream = stream_agent_answer([], [], [], llm, cancel=lambda: cancelled[0])
    assert next(stream).content == "第一段"
    cancelled[0] = True
    events = list(stream)
    assert events[-1].kind == "failed"
    assert events[-1].metadata["code"] == "CANCELLED"
    assert text(events) == ""
    assert llm.closed == 1


def test_cancellation_closes_model_stream_before_failure_is_published():
    cancelled = [False]
    llm = ScriptedAnswer([("text", "第一段"), ("text", "不可见")])
    stream = stream_agent_answer([], [], [], llm, cancel=lambda: cancelled[0])
    assert next(stream).kind == "delta"
    cancelled[0] = True
    assert next(stream).kind == "failed"
    assert llm.closed == 1
    stream.close()


def test_cancellation_after_flushed_tail_still_prevents_success():
    cancelled = [False]
    llm = ScriptedAnswer([("text", "正常答案<")])
    stream = stream_agent_answer([], [], [], llm, cancel=lambda: cancelled[0])
    assert next(stream).content == "正常答案"
    assert next(stream).content == "<"
    cancelled[0] = True
    assert next(stream).metadata["code"] == "CANCELLED"


@pytest.mark.parametrize("reason", ["tool_calls", "function_call", "tool_use"])
def test_native_tool_finish_cannot_mark_a_visible_answer_successful(reason):
    llm = ScriptedAnswer([("text", "前缀"), ("finish", reason)])
    events = list(stream_agent_answer([], [], [], llm))
    assert text(events) == "前缀"
    assert events[-1].kind == "failed"
    assert events[-1].metadata["code"] == "OUTPUT_PROTOCOL_BLOCKED"
    assert len(llm.calls) == 1


def test_retry_uses_remaining_shared_deadline():
    now = [100.0]

    class TimedAnswer(ScriptedAnswer):
        def answer_stream(self, *args, **kwargs):
            try:
                yield from super().answer_stream(*args, **kwargs)
            finally:
                now[0] += 3

    llm = TimedAnswer([("text", "<tool_call>bad</tool_call>")],
                      [("text", "修复结果"), ("finish", "stop")])
    events = list(stream_agent_answer([], [], [], llm, timeout=10, deadline=108,
                                      clock=lambda: now[0]))
    assert [call[1] for call in llm.calls] == [8, 5]
    assert events[-1].kind == "done"


def test_exhausted_deadline_does_not_start_repair():
    now = [1.0]

    class ExpiringAnswer(ScriptedAnswer):
        def answer_stream(self, *args, **kwargs):
            for event in super().answer_stream(*args, **kwargs):
                now[0] = 6.0
                yield event

    llm = ExpiringAnswer([("text", "<tool_call>bad</tool_call>")])
    events = list(stream_agent_answer([], [], [], llm, deadline=5, clock=lambda: now[0]))
    assert events[-1].kind == "failed"
    assert events[-1].metadata["code"] == "DEADLINE_EXCEEDED"
    assert len(llm.calls) == llm.closed == 1


def test_legacy_fake_client_without_answer_stream_remains_supported():
    class Fake:
        def chat_stream(self, messages, *, timeout=None, max_retries=None):
            yield "content", "普通回答"

    events = list(stream_agent_answer([], [], [], Fake()))
    assert text(events) == "普通回答"
    assert events[-1].kind == "done"
    assert events[-1].metadata == {"finish_reason": None, "repair_count": 0}


@pytest.mark.parametrize("reason,code", [
    ("unknown", "OUTPUT_INCOMPLETE"), (None, "OUTPUT_INCOMPLETE"),
    ("pause_turn", "OUTPUT_INCOMPLETE"), ("future_reason", "OUTPUT_INCOMPLETE"),
    ("content_filter", "OUTPUT_BLOCKED"), ("refusal", "OUTPUT_BLOCKED"),
])
def test_only_successful_upstream_terminal_reasons_accept_answers(reason, code):
    llm = ScriptedAnswer([("text", "尚未验收的答案"), ("finish", reason)])
    events = list(stream_agent_answer([], [], [], llm))
    assert events[-1].kind == "failed"
    assert events[-1].metadata["code"] == code
    assert events[-1].metadata["finish_reason"] == reason
    assert len(llm.calls) == 1


def test_native_stream_without_terminal_event_is_incomplete():
    llm = ScriptedAnswer([("text", "断开的传输")])
    events = list(stream_agent_answer([], [], [], llm))
    assert events[-1].kind == "failed"
    assert events[-1].metadata["code"] == "OUTPUT_INCOMPLETE"


@pytest.mark.parametrize("reason", ["stop", "end_turn", "stop_sequence"])
def test_successful_provider_terminal_reasons_are_accepted(reason):
    llm = ScriptedAnswer([("text", "完整答案"), ("finish", reason)])
    events = list(stream_agent_answer([], [], [], llm))
    assert events[-1].kind == "done"


@pytest.mark.parametrize("protocol", [
    '{"tool_calls":[{"function":{"name":"recall_item","arguments":"private"}}]}',
    '{"tool":"recall_item","args":{"id":"private"}}',
    '{"args":{"id":"private"},"tool":"recall_item"}',
    '```json\n{"tool":"recall_item","args":{"id":"private"}}\n```',
])
@pytest.mark.parametrize("width", [1, 11, 1000])
def test_json_pseudo_calls_cannot_be_published_as_answers(protocol, width):
    llm = ScriptedAnswer(
        [("text", protocol[i:i + width]) for i in range(0, len(protocol), width)],
        [("text", "修复结果"), ("finish", "stop")],
    )
    events = list(stream_agent_answer([], [], [], llm))
    assert text(events) == "修复结果"
    assert events[-1].kind == "done"
    assert events[-1].metadata["repair_count"] == 1


def test_requested_json_protocol_example_is_preserved():
    example = '```json\n{"tool":"recall_item","args":{"id":1}}\n```'
    llm = ScriptedAnswer([("text", char) for char in example] + [("finish", "stop")])
    events = list(stream_agent_answer(
        [{"role": "user", "content": "请给出工具调用的 JSON 示例"}], [], [], llm,
    ))
    assert text(events) == example
    assert events[-1].kind == "done"
