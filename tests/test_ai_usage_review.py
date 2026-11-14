"""Producer contract and final-usage regressions from the desktop-pet review."""
import json
from pathlib import Path
from uuid import UUID

import httpx
import pytest

from tests.test_ai_usage import Body, MESSAGES, adapter, chunk, collect, completion, sse


@pytest.mark.parametrize("model,provider,want_provider", [
    ("[体验]deepseek-v4.1-flash", "", "deepseek"),
    ("[体验]custom/model?mode=fast", "", "unknown"),
    ("⌘custom/model#fast", "[代理]gateway", "[代理]gateway"),
    (None, "", "unknown"),
])
def test_producer_names_remain_backend_compatible_without_losing_known_prefix(model, provider, want_provider):
    llm = adapter("openai", lambda _: httpx.Response(200, json=completion({
        "prompt_tokens": 34, "completion_tokens": 16, "total_tokens": 50}, model=model)))
    llm.client._client.usage_model = model
    llm.client._client.usage_provider = provider
    with collect() as collector:
        llm.chat(MESSAGES)
        llm.chat(MESSAGES)
    report = json.loads(collector.to_bytes())
    assert report["complete"] is True
    assert len(report["attempts"]) == 2
    for receipt in report["attempts"]:
        # B UsageReport.Parser requires non-control strings at these bounds;
        # null in the first attempt would discard it and the following suffix.
        assert receipt["provider"] == want_provider
        assert receipt["model"] == (model or "unknown")
        assert len(receipt["provider"].encode("utf-16-le")) // 2 <= 128
        assert len(receipt["model"].encode("utf-16-le")) // 2 <= 256
        assert (receipt["input_tokens"], receipt["output_tokens"], receipt["total_tokens"]) == (34, 16, 50)
    assert "private" not in collector.to_bytes().decode()
    assert "test-secret" not in collector.to_bytes().decode()


@pytest.mark.parametrize("method", ["chat_stream", "answer_stream"])
@pytest.mark.parametrize("final_usage", [False, True])
def test_zero_usage_preceding_generation_is_partial_until_fresh_final_usage(method, final_usage):
    zero = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    events = [chunk(usage=zero), chunk(text="answer"), chunk(finish="stop")]
    if final_usage:
        events.append(chunk(usage=zero))
    llm = adapter("openai", lambda _: sse(*events, "[DONE]"))
    with collect() as collector:
        assert ("content", "answer") in list(getattr(llm, method)(MESSAGES))
    report = json.loads(collector.to_bytes())
    receipt = report["attempts"][0]
    assert report["complete"] is True  # All physical attempts were observed.
    assert receipt["status"] == "success"
    assert (receipt["input_tokens"], receipt["output_tokens"], receipt["total_tokens"]) == (0, 0, 0)
    assert receipt["usage_state"] == ("complete" if final_usage else "partial")


def test_anthropic_sse_message_stop_without_data_type_completes_final_usage():
    events = [
        {"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant",
         "model": "claude-test", "content": [], "stop_reason": None, "stop_sequence": None,
         "usage": {"input_tokens": 3, "output_tokens": 1}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "answer"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
         "usage": {"output_tokens": 5}}]
    normal = sse(*events)
    pieces = normal.stream.pieces + [b"event: message_stop\ndata: {}\n\n"]
    llm = adapter("anthropic", lambda _: httpx.Response(200, headers={"content-type": "text/event-stream"},
                                                     stream=Body(pieces)))
    with collect() as collector:
        assert list(llm.answer_stream(MESSAGES, thinking_budget=0)) == [("content", "answer"), ("finish", "end_turn")]
    receipt = json.loads(collector.to_bytes())["attempts"][0]
    assert receipt["total_tokens"] == 8
    assert receipt["usage_state"] == "complete"


def test_real_producer_matches_shareable_backend_parser_fixture():
    fixture = json.loads((Path(__file__).parent / "fixtures" / "ai-usage-name-contract.json").read_text(encoding="utf-8"))
    with collect() as collector:
        for expected in fixture["attempts"]:
            model = expected["model"]
            llm = adapter("openai", lambda _, model=model: httpx.Response(200, json=completion({
                "prompt_tokens": 34, "completion_tokens": 16, "total_tokens": 50}, model=model)))
            llm.client._client.usage_model = model
            llm.chat(MESSAGES)
            llm.client.close()
    report = json.loads(collector.to_bytes())
    assert str(UUID(report["invocation_id"])) == report["invocation_id"]
    report["invocation_id"] = fixture["invocation_id"]
    assert len({receipt["id"] for receipt in report["attempts"]}) == 2
    for actual, expected in zip(report["attempts"], fixture["attempts"]):
        assert str(UUID(actual["id"])) == actual["id"]
        assert isinstance(actual["latency_ms"], int) and actual["latency_ms"] >= 0
        actual["id"], actual["latency_ms"] = expected["id"], expected["latency_ms"]
    assert report == fixture


@pytest.mark.parametrize("delta", [
    {"reasoning_content": "private thinking"},
    {"tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {
        "name": "read_record", "arguments": '{"record_id":1}'}}]},
])
def test_non_text_generation_also_invalidates_provisional_usage(delta):
    generation = chunk()
    generation["choices"][0]["delta"] = delta
    llm = adapter("openai", lambda _: sse(chunk(usage={"total_tokens": 0}), generation,
                                         chunk(finish="stop"), "[DONE]"))
    with collect() as collector:
        list(llm.chat_stream(MESSAGES))
    receipt = json.loads(collector.to_bytes())["attempts"][0]
    assert receipt["total_tokens"] == 0
    assert receipt["usage_state"] == "partial"
    assert "private thinking" not in collector.to_bytes().decode()


def test_role_metadata_alone_does_not_invalidate_explicit_zero_usage():
    role = chunk()
    role["choices"][0]["delta"] = {"role": "assistant"}
    llm = adapter("openai", lambda _: sse(chunk(usage={"total_tokens": 0}), role,
                                         chunk(finish="stop"), "[DONE]"))
    with collect() as collector:
        assert list(llm.chat_stream(MESSAGES)) == []
    assert json.loads(collector.to_bytes())["attempts"][0]["usage_state"] == "complete"


def test_usage_before_generation_in_same_event_still_covers_that_event():
    event = {"usage": {"total_tokens": 5}, **{k: v for k, v in chunk(text="answer").items() if k != "usage"}}
    llm = adapter("openai", lambda _: sse(event, chunk(finish="stop"), "[DONE]"))
    with collect() as collector:
        assert list(llm.chat_stream(MESSAGES)) == [("content", "answer")]
    receipt = json.loads(collector.to_bytes())["attempts"][0]
    assert receipt["total_tokens"] == 5
    assert receipt["usage_state"] == "complete"


def test_input_only_update_cannot_finalize_stale_generation_usage():
    llm = adapter("openai", lambda _: sse(chunk(usage={"total_tokens": 0}), chunk(text="answer"),
        chunk(usage={"prompt_tokens": 3}), chunk(finish="stop"), "[DONE]"))
    with collect() as collector:
        list(llm.chat_stream(MESSAGES))
    receipt = json.loads(collector.to_bytes())["attempts"][0]
    assert receipt["input_tokens"] == 3
    assert receipt["total_tokens"] == 0
    assert receipt["usage_state"] == "partial"
