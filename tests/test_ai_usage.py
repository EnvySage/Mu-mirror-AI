"""Real SDK/HTTP tests: receipts describe network attempts, never inferred calls."""
import json
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import anthropic
import httpx
import openai
import pytest

from llm.openai_llm import OpenAiLlm
from llm.anthropic_llm import AnthropicLlm

MESSAGES = [{"role": "user", "content": "private user text"}]


def usage_module():
    try:
        from mirror_core import usage
        return usage
    except ImportError:
        pytest.fail("request-local usage capture is not implemented")


def completion(usage=None, model="cn:deepseek-v4.1-flash"):
    body = {"id": "chat_1", "object": "chat.completion", "created": 1, "model": model,
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": "private answer"}}]}
    if usage is not None:
        body["usage"] = usage
    return body


def adapter(protocol, handler, *, retries=0):
    usage = usage_module()
    if protocol == "openai":
        llm = OpenAiLlm("test-secret", "https://test/v1", "cn:deepseek-v4.1-flash", max_retries=retries)
        cls, sdk = usage.OpenAIUsageClient, openai.OpenAI
    else:
        llm = AnthropicLlm("test-secret", "https://test/v1", "claude-test", max_retries=retries)
        cls, sdk = usage.AnthropicUsageClient, anthropic.Anthropic
    llm.client.close()
    http_client = cls(model=llm.model, provider="", transport=network_transport(cls, handler))
    llm.client = sdk(api_key="test-secret", base_url="https://test/v1", http_client=http_client,
                     max_retries=retries)
    return llm


def network_transport(cls, handler):
    # OpenAI 3 uses httpx2 by default; older supported SDKs use httpx.
    if not issubclass(cls, httpx.Client):
        import httpx2
        class Stream(httpx2.SyncByteStream):
            def __init__(self, wrapped):
                self.wrapped = wrapped
            def __iter__(self):
                yield from self.wrapped
            def close(self):
                self.wrapped.close()
        def serve(request):
            response = handler(request)
            kwargs = {"content": response.content} if response.is_stream_consumed else {"stream": Stream(response.stream)}
            return httpx2.Response(response.status_code, headers=response.headers, **kwargs)
        transport = httpx2.MockTransport(serve)
    else:
        transport = httpx.MockTransport(handler)
    return transport


@contextmanager
def collect():
    usage = usage_module()
    collector = usage.UsageCollector(str(uuid4()))
    with usage.bind_collector(collector):
        yield collector


def test_unary_records_actual_usage_without_content_or_credentials():
    # Removing HTTP-boundary capture must lose this physical receipt.
    llm = adapter("openai", lambda _: httpx.Response(200, json=completion({
        "prompt_tokens": 34, "completion_tokens": 16, "total_tokens": 50,
        "credit": 0, "quota": {"percent": 17}})))
    with collect() as collector:
        assert llm.chat(MESSAGES) == "private answer"
    report = json.loads(collector.to_bytes())
    assert report["complete"] is True
    assert len(report["attempts"]) == 1
    attempt = report["attempts"][0]
    assert (attempt["input_tokens"], attempt["output_tokens"], attempt["total_tokens"]) == (34, 16, 50)
    assert (attempt["status"], attempt["usage_state"], attempt["http_status"]) == ("success", "complete", 200)
    assert attempt["provider"] == "deepseek"
    assert attempt["credits"] is None
    assert "private" not in collector.to_bytes().decode()
    assert "test-secret" not in collector.to_bytes().decode()


class Body(httpx.SyncByteStream):
    def __init__(self, pieces, *, fail=False):
        self.pieces, self.fail = pieces, fail

    def __iter__(self):
        yield from self.pieces
        if self.fail:
            raise httpx.ReadError("simulated disconnect")


def sse(*events, fail=False):
    pieces = [(("event: " + event["type"] + "\n" if isinstance(event, dict) and "type" in event else "")
               + "data: " + (event if isinstance(event, str) else json.dumps(event)) + "\n\n").encode()
              for event in events]
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Body(pieces, fail=fail))


def chunk(*, text=None, usage=None, finish=None):
    return {"id": "chat_1", "object": "chat.completion.chunk", "created": 1,
            "model": "cn:deepseek-v4.1-flash", "choices": [] if usage is not None else [
                {"index": 0, "delta": {"content": text}, "finish_reason": finish}], "usage": usage}


@pytest.mark.parametrize("method", ["chat_stream", "answer_stream"])
def test_empty_choice_final_usage_chunk_is_retained_and_requested(method):
    bodies = []
    def handler(request):
        bodies.append(json.loads(request.content))
        return sse(chunk(text="answer"), chunk(finish="stop"), chunk(usage={
            "prompt_tokens": 34, "completion_tokens": 16, "total_tokens": 50}), "[DONE]")
    llm = adapter("openai", handler)
    with collect() as collector:
        events = list(getattr(llm, method)(MESSAGES))
    assert ("content", "answer") in events
    receipt = json.loads(collector.to_bytes())["attempts"][0]
    assert (receipt["total_tokens"], receipt["status"], receipt["usage_state"]) == (50, "success", "complete")
    assert bodies[0]["stream_options"] == {"include_usage": True}


def test_sdk_hidden_retry_is_a_separate_unknown_error_attempt():
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, json={"error": {"message": "retry", "type": "rate_limit"}},
                                  headers={"retry-after-ms": "1"})
        return httpx.Response(200, json=completion({"total_tokens": 9}))
    llm = adapter("openai", handler, retries=1)
    with collect() as collector:
        assert llm.chat(MESSAGES) == "private answer"
    attempts = json.loads(collector.to_bytes())["attempts"]
    assert len(attempts) == 2
    assert attempts[0]["status"] == "error"
    assert attempts[0]["total_tokens"] is None
    assert attempts[0]["usage_state"] == "unknown"
    assert attempts[1]["total_tokens"] == 9
    assert attempts[1]["input_tokens"] is None
    assert attempts[0]["id"] != attempts[1]["id"]


def test_interrupted_stream_preserves_known_counts_without_replaying():
    calls = []
    def handler(request):
        calls.append(request)
        return sse(chunk(text="partial answer"), chunk(usage={"prompt_tokens": 12}), fail=True)
    llm = adapter("openai", handler, retries=1)
    with collect() as collector:
        stream = llm.chat_stream(MESSAGES)
        assert next(stream) == ("content", "partial answer")
        with pytest.raises(Exception):
            list(stream)
    assert len(calls) == 1
    receipt = json.loads(collector.to_bytes())["attempts"][0]
    assert (receipt["input_tokens"], receipt["output_tokens"], receipt["total_tokens"]) == (12, None, None)
    assert (receipt["status"], receipt["usage_state"]) == ("error", "partial")


def test_close_before_final_chunk_is_cancelled_and_unknown():
    llm = adapter("openai", lambda _: sse(chunk(text="partial"), chunk(usage={"total_tokens": 90}), "[DONE]"))
    with collect() as collector:
        stream = llm.chat_stream(MESSAGES)
        assert next(stream) == ("content", "partial")
        stream.close()
    receipt = json.loads(collector.to_bytes())["attempts"][0]
    assert (receipt["status"], receipt["total_tokens"], receipt["usage_state"]) == ("cancelled", None, "unknown")


def test_anthropic_cumulative_stream_counts_and_cache_are_not_added_twice():
    events = [
        {"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant",
         "model": "claude-test", "content": [], "stop_reason": None, "stop_sequence": None,
         "usage": {"input_tokens": 10, "output_tokens": 1, "cache_read_input_tokens": 20,
                   "cache_creation_input_tokens": 5}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "answer"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
         "usage": {"output_tokens": 7}}, {"type": "message_stop"}]
    llm = adapter("anthropic", lambda _: sse(*events))
    with collect() as collector:
        assert list(llm.answer_stream(MESSAGES, thinking_budget=0)) == [("content", "answer"), ("finish", "end_turn")]
    receipt = json.loads(collector.to_bytes())["attempts"][0]
    assert (receipt["input_tokens"], receipt["output_tokens"], receipt["total_tokens"]) == (35, 7, 42)
    assert receipt["cache_read_tokens"] == 20
    assert receipt["cache_creation_tokens"] == 5
    assert receipt["usage_state"] == "complete"


@pytest.mark.parametrize("fields,expected", [
    ({"credits_consumed": "0.100000000001"}, "0.100000000001"),
    ({"points_consumed": "0.200000000001"}, "0.200000000001"),
    ({"consumed_points": "0.300000000001"}, "0.300000000001"),
    ({"consumed_credits": "0"}, "0"),
    ({"consumed_credits": "-0.00"}, "0.00"),
    ({"credit": 0, "quota": {"percent": 17}, "balance": 50}, None),
    ({"credits_consumed": "NaN"}, None), ({"credits_consumed": "-1"}, None),
    ({"credits_consumed": True}, None)])
def test_only_explicit_consumption_fields_become_decimal_credits(fields, expected):
    llm = adapter("openai", lambda _: httpx.Response(200, json=completion(fields)))
    with collect() as collector:
        llm.chat(MESSAGES)
    receipt = json.loads(collector.to_bytes())["attempts"][0]
    assert receipt["credits"] == expected
    assert receipt["credit_unit"] == ("积分" if expected is not None else None)
    assert receipt["total_tokens"] is None


def test_two_concurrent_requests_using_one_client_do_not_mix_receipts():
    barrier = Barrier(2)
    def handler(request):
        barrier.wait(timeout=5)
        return httpx.Response(200, json=completion({"total_tokens": int(json.loads(request.content)["temperature"])}))
    llm = adapter("openai", handler)
    def run(count):
        with collect() as collector:
            llm.chat(MESSAGES, temperature=count)
        return json.loads(collector.to_bytes())
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, [1, 2]))
    assert [r["attempts"][0]["total_tokens"] for r in results] == [1, 2]
    assert results[0]["invocation_id"] != results[1]["invocation_id"]


def test_missing_metadata_does_not_add_stream_options_or_attempts():
    bodies = []
    def handler(request):
        bodies.append(json.loads(request.content))
        return sse(chunk(text="answer"), "[DONE]")
    llm = adapter("openai", handler)
    assert list(llm.chat_stream(MESSAGES)) == [("content", "answer")]
    assert "stream_options" not in bodies[0]


def test_unsupported_stream_usage_option_falls_back_once_before_generation():
    bodies = []
    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if "stream_options" in body:
            return httpx.Response(400, json={"error": {"message": "unknown parameter stream_options", "type": "invalid_request_error"}})
        return sse(chunk(text="answer"), "[DONE]")
    llm = adapter("openai", handler)
    with collect() as collector:
        assert list(llm.chat_stream(MESSAGES)) == [("content", "answer")]
    assert len(bodies) == 2
    assert "stream_options" not in bodies[1]
    receipts = json.loads(collector.to_bytes())["attempts"]
    assert [r["status"] for r in receipts] == ["error", "success"]
    assert all(r["usage_state"] == "unknown" for r in receipts)


def test_json_and_native_tool_planning_are_observed_through_same_boundary():
    replies = iter(["{}", "ready"])
    def handler(request):
        body = completion({"total_tokens": 4})
        body["choices"][0]["message"]["content"] = next(replies)
        return httpx.Response(200, json=body)
    llm = adapter("openai", handler)
    with collect() as collector:
        assert llm.json_task(MESSAGES) == "{}"
        assert llm.plan(MESSAGES, [], protocol="native")["done"] is True
    assert [r["total_tokens"] for r in json.loads(collector.to_bytes())["attempts"]] == [4, 4]


def test_attempt_count_overflow_preserves_known_prefix_and_marks_incomplete():
    llm = adapter("openai", lambda _: httpx.Response(200, json=completion({"total_tokens": 1})))
    with collect() as collector:
        for _ in range(101):
            llm.chat(MESSAGES)
    report = json.loads(collector.to_bytes())
    assert len(report["attempts"]) == 100
    assert report["complete"] is False
    assert sum(r["total_tokens"] for r in report["attempts"]) == 100


def test_byte_overflow_keeps_valid_utf8_json_below_transport_budget():
    llm = adapter("openai", lambda _: httpx.Response(200, json=completion({
        "prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2,
        "credits_consumed": "0.123456789012", "credit_unit": "积" * 32,
        "prompt_tokens_details": {"cached_tokens": 1},
        "completion_tokens_details": {"reasoning_tokens": 1}}, model="qwen-" + "x" * 250)))
    with collect() as collector:
        for _ in range(100):
            llm.chat(MESSAGES)
    payload = collector.to_bytes()
    assert len(payload) <= 48 * 1024
    report = json.loads(payload)
    assert report["complete"] is False
    assert 0 < len(report["attempts"]) < 100
    assert all(r["total_tokens"] == 2 for r in report["attempts"])


def test_usage_like_text_inside_answer_is_ignored_across_byte_boundaries():
    body = completion({"total_tokens": 7})
    body["choices"][0]["message"]["content"] = 'private\\\" usage: {"total_tokens":9999999}' * 200
    raw = json.dumps(body).encode()
    llm = adapter("openai", lambda _: httpx.Response(200, headers={"content-type": "application/json"},
        stream=Body([raw[i:i+17] for i in range(0, len(raw), 17)])))
    with collect() as collector:
        assert llm.chat(MESSAGES) == body["choices"][0]["message"]["content"]
    assert json.loads(collector.to_bytes())["attempts"][0]["total_tokens"] == 7


def test_numeric_credit_json_preserves_decimal_digits_exactly():
    raw = json.dumps(completion()).replace('"choices":', '"usage":{"credits_consumed":1234567890123456789.123456789012},"choices":')
    llm = adapter("openai", lambda _: httpx.Response(200, content=raw, headers={"content-type": "application/json"}))
    with collect() as collector:
        llm.chat(MESSAGES)
    assert json.loads(collector.to_bytes())["attempts"][0]["credits"] == "1234567890123456789.123456789012"


def test_silent_stream_eof_cannot_turn_provisional_usage_into_complete_usage():
    llm = adapter("openai", lambda _: sse(chunk(text="partial"), chunk(usage={"total_tokens": 5})))
    with collect() as collector:
        assert list(llm.chat_stream(MESSAGES)) == [("content", "partial")]
    receipt = json.loads(collector.to_bytes())["attempts"][0]
    assert receipt["total_tokens"] == 5
    assert receipt["usage_state"] == "partial"


def test_non_capability_stream_error_is_not_retried_for_usage():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(400, json={"error": {"message": "stream_options request budget exhausted", "type": "invalid_request_error"}})
    llm = adapter("openai", handler)
    with collect() as collector:
        with pytest.raises(Exception):
            list(llm.chat_stream(MESSAGES))
    assert len(calls) == 1
    assert len(json.loads(collector.to_bytes())["attempts"]) == 1


def test_openai_cache_and_reasoning_are_details_not_additional_total_tokens():
    llm = adapter("openai", lambda _: httpx.Response(200, json=completion({
        "prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110,
        "prompt_tokens_details": {"cached_tokens": 11},
        "completion_tokens_details": {"reasoning_tokens": 3}})))
    with collect() as collector:
        llm.chat(MESSAGES)
    receipt = json.loads(collector.to_bytes())["attempts"][0]
    assert receipt["total_tokens"] == 110
    assert receipt["cache_read_tokens"] == 11
    assert receipt["reasoning_tokens"] == 3


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_factory_installs_capture_and_preserves_explicit_unknown_model_provider(protocol, monkeypatch):
    from llm.factory import create_llm
    from generated import common_pb2
    from llm import openai_llm, anthropic_llm
    usage = usage_module()
    if protocol == "openai":
        cls, module, attribute = usage.OpenAIUsageClient, openai_llm, "OpenAIUsageClient"
        body = completion({"total_tokens": 8}, model="custom-model")
        number = common_pb2.OPENAI
    else:
        cls, module, attribute = usage.AnthropicUsageClient, anthropic_llm, "AnthropicUsageClient"
        body = {"id": "msg_1", "type": "message", "role": "assistant", "model": "custom-model",
                "content": [{"type": "text", "text": "answer"}], "stop_reason": "end_turn",
                "stop_sequence": None, "usage": {"input_tokens": 3, "output_tokens": 5}}
        number = common_pb2.ANTHROPIC
    transport = network_transport(cls, lambda _: httpx.Response(200, json=body))
    monkeypatch.setattr(module, attribute, lambda **kwargs: cls(transport=transport, **kwargs))
    llm = create_llm(provider="custom-provider", api_key="test", base_url="https://test/v1",
                     model="custom-model", protocol=number, max_retries=0)
    with collect() as collector:
        assert llm.chat(MESSAGES)
    llm.client.close()
    receipt = json.loads(collector.to_bytes())["attempts"][0]
    assert receipt["total_tokens"] == 8
    assert receipt["provider"] == "custom-provider"


def test_multiline_crlf_sse_usage_survives_partial_network_frames():
    raw = ("data: {\r\ndata: \"id\":\"c1\",\"object\":\"chat.completion.chunk\",\"created\":1,\r\n"
           "data: \"model\":\"qwen-test\",\"choices\":[],\r\n"
           "data: \"usage\":{\"prompt_tokens\":2,\"completion_tokens\":3,\"total_tokens\":5}}\r\n\r\n"
           "data: [DONE]\r\n\r\n").encode()
    llm = adapter("openai", lambda _: httpx.Response(200, headers={"content-type": "text/event-stream"},
        stream=Body([raw[i:i+11] for i in range(0, len(raw), 11)])))
    with collect() as collector:
        assert list(llm.chat_stream(MESSAGES)) == []
    receipt = json.loads(collector.to_bytes())["attempts"][0]
    assert receipt["total_tokens"] == 5
    assert receipt["usage_state"] == "complete"


@pytest.mark.parametrize("client_name", ["OpenAIUsageClient", "AnthropicUsageClient"])
def test_observed_client_retains_sdk_garbage_collection_cleanup(client_name):
    # SDK-owned default clients close on finalization; supplying a bare httpx
    # default client must not leave sockets open for legacy per-call adapters.
    import gc
    from threading import Event
    from weakref import ref
    cls = getattr(usage_module(), client_name)
    transport = network_transport(cls, lambda _: httpx.Response(200))
    closed = Event()
    original_close = transport.close
    def close():
        original_close()
        closed.set()
    transport.close = close
    client = cls(model="test", transport=transport)
    reference = ref(client)
    del client
    gc.collect()
    assert reference() is None
    assert closed.is_set()
