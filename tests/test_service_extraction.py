"""Shared AI service functions must preserve the current legacy wire behavior."""

import json
from types import SimpleNamespace

from generated import common_pb2
from generated import mirror_chat_pb2 as pb2
from services import answer_service, intent_service, plan_service


def test_extract_intent_keeps_legacy_whitelists_and_query_fallback(monkeypatch):
    prompts = []

    class FakeLlm:
        def json_task(self, messages):
            prompts.extend(messages)
            return json.dumps({
                "query_type": "not-a-route",
                "content_type": "not-a-content-type",
                "moods": ["happy", "not-a-mood"],
                "time_range": "last week",
                "rewritten_query": "",
            })

    config = SimpleNamespace(
        provider="openai", api_key="key", base_url="https://api.test",
        model="test-model", protocol="openai",
    )
    monkeypatch.setattr(intent_service.loader, "render", lambda name, **kwargs: f"{name}:{kwargs}")
    monkeypatch.setattr(intent_service, "format_glossary", lambda glossary: "")

    result = intent_service.extract_intent(
        "原始问题", [], config, llm_factory=lambda **kwargs: FakeLlm()
    )

    assert result.query_type == "hybrid"
    assert result.content_type is None
    assert result.moods == ["happy"]
    assert result.time_range == "last week"
    assert result.rewritten_query == "原始问题"
    assert prompts == [{"role": "user", "content": "intent:{'query': '原始问题', 'glossary': ''}"}]


def test_build_answer_messages_preserves_legacy_history_and_empty_glossary(monkeypatch):
    rendered = {}

    def render(name, **kwargs):
        rendered["name"] = name
        rendered.update(kwargs)
        return "rendered legacy prompt"

    monkeypatch.setattr(answer_service.loader, "render", render)
    monkeypatch.setattr(answer_service, "format_glossary", lambda glossary: "")
    history = [SimpleNamespace(role="assistant", content="之前聊到的方案")]

    messages = answer_service.build_answer_messages(
        "继续说", history, [], [], [],
    )

    assert messages == [
        {"role": "system", "content": "rendered legacy prompt"},
        {"role": "assistant", "content": "之前聊到的方案"},
        {"role": "user", "content": "继续说"},
    ]
    assert rendered == {
        "name": "chat",
        "question": "继续说",
        "history": "assistant: 之前聊到的方案",
        "context": "（没有找到相关记录）",
        "glossary": "",
        "tool_results": "",
    }


def test_stream_answer_preserves_event_order_cross_chunk_filter_and_source_numbers():
    chunks = [
        SimpleNamespace(
            record_id=i, title=f"标题{i}", content=f"正文{i}", created_at=f"2026-10-{i:02}",
        )
        for i in range(1, 6)
    ]
    tool_results = [SimpleNamespace(
        tool="find_item", success=True, payload_json='{"item":{"vault_item_id":9}}', summary="文件",
    ), SimpleNamespace(
        tool="get_stats", success=True, payload_json="{}", summary="统计",
    )]

    class FakeLlm:
        def chat_stream(self, messages):
            yield "thinking", "先思考"
            yield "content", "引用 [1"
            yield "content", "] 和 [5] [F1] [get_"
            yield "content", "stats]"

    events = list(answer_service.stream_answer(
        [{"role": "user", "content": "问题"}], chunks, tool_results, FakeLlm(),
    ))

    assert [event.kind for event in events] == ["thinking", "delta", "delta", "done"]
    assert events[0].content == "先思考"
    assert "".join(event.content for event in events if event.kind == "delta") == "引用 [1] 和 [5] [F1]"
    assert [source.n for source in events[-1].sources] == [1, 5]
    assert [source.record_id for source in events[-1].sources] == [1, 5]


def test_legacy_chat_servicer_translates_shared_answer_events_without_wire_changes(monkeypatch):
    from services import chat_service

    chunks = [
        pb2.RetrievedChunk(
            record_id=i, title=f"标题{i}", content=f"正文{i}", created_at=f"2026-10-{i:02}",
            content_type="thought",
        )
        for i in range(1, 4)
    ]
    tool_results = [common_pb2.ToolResult(tool="get_stats", success=True, payload_json="{}")]

    class FakeLlm:
        def chat_stream(self, messages):
            assert messages == [
                {"role": "system", "content": "legacy prompt"},
                {"role": "assistant", "content": "既有上下文"},
                {"role": "user", "content": "问题"},
            ]
            yield "thinking", "先想"
            yield "content", "见 [1"
            yield "content", "] 和 [3] [get_"
            yield "content", "stats]"

    monkeypatch.setattr(chat_service, "create_llm", lambda **kwargs: FakeLlm())
    monkeypatch.setattr(answer_service.loader, "render", lambda name, **kwargs: "legacy prompt")
    monkeypatch.setattr(answer_service, "format_glossary", lambda glossary: "")
    request = pb2.ChatRequest(
        question="问题",
        history=[pb2.ChatMessage(role="assistant", content="既有上下文")],
        chunks=chunks,
        llm_config=common_pb2.LlmConfig(provider="openai", model="test", protocol=1),
        tool_results=tool_results,
    )

    chunks_out = list(chat_service.MirrorChatServicer().Chat(request, context=None))

    assert chunks_out[0].HasField("thinking") and chunks_out[0].thinking == "先想"
    assert "".join(chunk.content for chunk in chunks_out if chunk.content) == "见 [1] 和 [3]"
    assert chunks_out[-1].done is True
    assert [source.n for source in chunks_out[-1].sources] == [1, 3]


def test_plan_next_step_keeps_thinking_then_final_done_and_last_calls(monkeypatch):
    rendered = {}

    def render(name, **kwargs):
        rendered["name"] = name
        rendered.update(kwargs)
        return "plan prompt"

    monkeypatch.setattr(plan_service.loader, "render", render)

    class FakeLlm:
        def chat_stream(self, messages, **kwargs):
            yield "thinking", "先判断"
            yield "content", '{"calls":[{"tool":"search_records","args":{"query":"方案"}}],"done":true}'

    request = SimpleNamespace(
        question="找方案", tools=[SimpleNamespace(name="search_records", description="搜索", args_schema="{}")],
        glossary=[], history=[], previous_results=[], step=2, max_steps=4, has_retrieval=True,
    )
    events = list(plan_service.plan_next_step(request, FakeLlm()))

    assert [event.kind for event in events] == ["thinking", "final"]
    assert events[0].content == "先判断"
    assert events[-1].final is True
    assert events[-1].done is True
    assert [(call.tool, json.loads(call.args_json)) for call in events[-1].calls] == [
        ("search_records", {"query": "方案"}),
    ]
    assert rendered["name"] == "plan_tools"
    assert rendered["step"] == 2


def test_plan_tools_uses_legacy_single_prompt_and_non_streaming_sdk_call(monkeypatch):
    rendered = {}

    def render(name, **kwargs):
        rendered["name"] = name
        rendered.update(kwargs)
        return "legacy single-step prompt"

    monkeypatch.setattr(plan_service.loader, "render", render)

    class FakeLlm:
        def json_task(self, messages):
            assert messages == [{"role": "user", "content": "legacy single-step prompt"}]
            return '{"calls":[{"tool":"search_records","args":{}}]}'

        def chat_stream(self, *args, **kwargs):
            raise AssertionError("legacy PlanTools must not switch to streaming")

    request = SimpleNamespace(
        question="找记录", tools=[SimpleNamespace(name="search_records", description="搜索", args_schema="{}")],
        glossary=[],
    )
    events = list(plan_service.plan_next_step(request, FakeLlm(), prompt_name="plan_tools_single"))

    assert len(events) == 1
    assert events[0].final is True
    assert events[0].done is True
    assert [call.tool for call in events[0].calls] == ["search_records"]
    assert rendered["name"] == "plan_tools_single"

\n