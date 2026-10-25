"""Graph planning contracts exercised through real SDKs and synthetic HTTP responses."""
import json

import httpx
import pytest
import anthropic
import openai

from llm.anthropic_llm import AnthropicLlm
from llm.openai_llm import OpenAiLlm
from mirror_core.errors import AiServiceError, LlmTimeoutError

MESSAGES = [{"role": "user", "content": "Read file 7"}]
TOOLS = [{"name": "recall_item", "description": "Read authorized body", "parameters": {
    "type": "object", "properties": {"vault_item_id": {"type": "integer"}},
    "required": ["vault_item_id"], "additionalProperties": False,
}}]
PLAN = {"calls": [{"tool": "recall_item", "args": {"vault_item_id": 7}}], "done": False, "reason": "read body"}


def response(provider, *, native=True, text=None, finish=None):
    if provider == "openai":
        message = {"role": "assistant", "content": text}
        if native:
            message["tool_calls"] = [{"id": "call_7", "type": "function", "function": {
                "name": "recall_item", "arguments": '{"vault_item_id":7}',
            }}]
        return {"id": "chat_1", "object": "chat.completion", "created": 1, "model": "test",
                "choices": [{"index": 0, "finish_reason": finish or ("tool_calls" if native else "stop"), "message": message}]}
    content = [{"type": "tool_use", "id": "call_7", "name": "recall_item", "input": {"vault_item_id": 7}}] if native else [{"type": "text", "text": text or ""}]
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "test", "content": content,
            "stop_reason": finish or ("tool_use" if native else "end_turn"), "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 10}}


def adapter(provider, handler):
    transport = httpx.MockTransport(handler)
    llm = (OpenAiLlm("test", "https://test/v1", "test") if provider == "openai"
           else AnthropicLlm("test", "https://test", "test"))
    llm.client.close()
    client = httpx.Client(transport=transport)
    llm.client = (openai.OpenAI(api_key="test", base_url="https://test/v1", http_client=client)
                  if provider == "openai" else anthropic.Anthropic(api_key="test", base_url="https://test", http_client=client))
    return llm


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_native_calls_are_decoded_from_sdk_structured_payload(provider):
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=response(provider))
    llm = adapter(provider, handler)
    result = llm.plan(MESSAGES, TOOLS, timeout=8)
    assert result["calls"] == [{"tool": "recall_item", "args": {"vault_item_id": 7}}]
    assert result["done"] is False
    assert result["protocol"] == "native"
    assert result["finish_reason"] == ("tool_calls" if provider == "openai" else "tool_use")
    sent = requests[0]["tools"][0]
    assert (sent["function"]["parameters"] if provider == "openai" else sent["input_schema"]) == TOOLS[0]["parameters"]


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_auto_falls_back_only_for_unsupported_tools_with_remaining_deadline(provider, monkeypatch):
    import llm.planning as planning
    now = [100.0]
    monkeypatch.setattr(planning.time, "monotonic", lambda: now[0])
    requests = []
    timeouts = []
    def handler(request):
        requests.append(json.loads(request.content))
        timeouts.append(request.extensions["timeout"]["read"])
        if len(requests) == 1:
            now[0] += 3
            return httpx.Response(400, json={"error": {"type": "invalid_request_error", "message": "tools is not supported by this model"}})
        return httpx.Response(200, json=response(provider, native=False, text=json.dumps(PLAN)))
    llm = adapter(provider, handler)
    result = llm.plan(MESSAGES, TOOLS, timeout=8)
    assert result == {**PLAN, "protocol": "text_json", "finish_reason": "stop" if provider == "openai" else "end_turn"}
    assert len(requests) == 2
    assert "tools" not in requests[1]
    assert requests[1]["max_tokens"] >= 4096
    assert timeouts == [8, 5]


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize("status,message", [(401, "tools not supported"), (429, "tools not supported"), (500, "tools not supported"), (400, "invalid tools schema"), (400, "Invalid tools schema: additionalProperties not supported")])
def test_errors_do_not_trigger_text_fallback(provider, status, message):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(status, json={"error": {"type": "invalid_request_error", "message": message}})
    with pytest.raises(AiServiceError):
        adapter(provider, handler).plan(MESSAGES, TOOLS, timeout=8)
    assert len(requests) == 1


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_explicit_json_schema_is_strict_and_does_not_silently_fallback(provider):
    wire_plan = {"calls": [{"tool": "recall_item", "args_json": '{"vault_item_id":7}'}], "done": False, "reason": "read body"}
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=response(provider, native=False, text=json.dumps(wire_plan)))
    result = adapter(provider, handler).plan(MESSAGES, TOOLS, protocol="json_schema")
    assert result["calls"] == PLAN["calls"]
    assert result["protocol"] == "json_schema"
    sent = requests[0]
    if provider == "openai":
        assert sent["response_format"]["type"] == "json_schema"
        assert sent["response_format"]["json_schema"]["strict"] is True
    else:
        assert sent["output_config"]["format"]["type"] == "json_schema"
    assert "tools" not in sent


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_explicit_schema_unsupported_fails_without_fallback(provider):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(400, json={"error": {"type": "invalid_request_error", "message": "json_schema not supported"}})
    with pytest.raises(AiServiceError, match="json_schema"):
        adapter(provider, handler).plan(MESSAGES, TOOLS, protocol="json_schema")
    assert len(requests) == 1


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_fallback_does_not_start_after_deadline(provider, monkeypatch):
    import llm.planning as planning
    now = [1.0]
    monkeypatch.setattr(planning.time, "monotonic", lambda: now[0])
    requests = []
    def handler(request):
        requests.append(request)
        now[0] = 10.0
        return httpx.Response(400, json={"error": {"type": "invalid_request_error", "message": "tools not supported"}})
    with pytest.raises(LlmTimeoutError):
        adapter(provider, handler).plan(MESSAGES, TOOLS, timeout=8)
    assert len(requests) == 1


def stream_response(provider, finish):
    if provider == "openai":
        chunks = [
            {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "test", "choices": [{"index": 0, "delta": {"reasoning_content": "consider"}, "finish_reason": None}]},
            {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "test", "choices": [{"index": 0, "delta": {"content": "answer"}, "finish_reason": None}]},
            {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "test", "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
        ]
        data = "".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n"
    else:
        events = [
            ("message_start", {"type": "message_start", "message": response(provider, native=False, text="")}),
            ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "consider"}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("content_block_start", {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "answer"}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 1}),
            ("message_delta", {"type": "message_delta", "delta": {"stop_reason": finish, "stop_sequence": None}, "usage": {"output_tokens": 10}}),
            ("message_stop", {"type": "message_stop"}),
        ]
        data = "".join("event: " + kind + "\ndata: " + json.dumps(value) + "\n\n" for kind, value in events)
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=data)


@pytest.mark.parametrize("provider,finish", [("openai", "stop"), ("openai", "length"), ("anthropic", "end_turn"), ("anthropic", "max_tokens")])
def test_answer_stream_exposes_finish_without_changing_legacy_events(provider, finish):
    llm = adapter(provider, lambda request: stream_response(provider, finish))
    assert list(llm.answer_stream(MESSAGES, thinking_budget=0)) == [("thinking", "consider"), ("content", "answer"), ("finish", finish)]
    assert list(llm.chat_stream(MESSAGES, thinking_budget=0)) == [("thinking", "consider"), ("content", "answer")]

@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_native_stops_without_tools_is_done(provider):
    result = adapter(provider, lambda request: httpx.Response(200, json=response(provider, native=False, text="Enough evidence"))).plan(MESSAGES, TOOLS)
    assert result == {"calls": [], "done": True, "reason": "Enough evidence", "protocol": "native", "finish_reason": "stop" if provider == "openai" else "end_turn"}


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_strict_schema_supports_the_actual_registry_without_provider_specific_constraints(provider):
    from agent_tools.registry import get_read_tool_specs
    tools = get_read_tool_specs()
    wire_plan = {"calls": [{"tool": "recall_item", "args_json": '{"vault_item_id":7}'}], "done": False, "reason": "read body"}
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=response(provider, native=False, text=json.dumps(wire_plan)))
    result = adapter(provider, handler).plan(MESSAGES, tools, protocol="json_schema")
    assert result["calls"] == [{"tool": "recall_item", "args": {"vault_item_id": 7}}]
    schema = requests[0]["response_format"]["json_schema"]["schema"] if provider == "openai" else requests[0]["output_config"]["format"]["schema"]
    call_schema = schema["properties"]["calls"]["items"]
    assert call_schema["properties"] == {"tool": {"type": "string", "enum": [tool["name"] for tool in tools]}, "args_json": {"type": "string"}}
    assert call_schema["required"] == ["tool", "args_json"]
    assert call_schema["additionalProperties"] is False
    assert schema["required"] == ["calls", "done", "reason"]


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_success_arriving_after_shared_deadline_is_not_accepted(provider, monkeypatch):
    import llm.planning as planning
    now = [1.0]
    monkeypatch.setattr(planning.time, "monotonic", lambda: now[0])
    def handler(request):
        now[0] = 10.0
        return httpx.Response(200, json=response(provider))
    with pytest.raises(LlmTimeoutError):
        adapter(provider, handler).plan(MESSAGES, TOOLS, timeout=8)

@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize("text", [json.dumps(PLAN), '```json\n' + json.dumps(PLAN) + '\n```', '<tool_call name="recall_item">{"vault_item_id":7}</tool_call>', '<function_calls><invoke name="recall_item"/></function_calls>'])
def test_native_text_pseudo_calls_are_invalid_and_never_text_fallback(provider, text):
    from mirror_core.errors import ContentInvalidError
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=response(provider, native=False, text=text))
    with pytest.raises(ContentInvalidError):
        adapter(provider, handler).plan(MESSAGES, TOOLS)
    assert len(requests) == 1

@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_native_arguments_must_be_an_object_without_fallback(provider):
    from mirror_core.errors import ContentInvalidError
    requests = []
    def handler(request):
        requests.append(request)
        payload = response(provider)
        if provider == "openai":
            payload["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = "[]"
        else:
            payload["content"][0]["input"] = []
        return httpx.Response(200, json=payload)
    with pytest.raises(ContentInvalidError):
        adapter(provider, handler).plan(MESSAGES, TOOLS)
    assert len(requests) == 1


def test_native_openai_malformed_argument_json_is_a_content_error():
    from mirror_core.errors import ContentInvalidError
    def handler(request):
        payload = response("openai")
        payload["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = "{"
        return httpx.Response(200, json=payload)
    with pytest.raises(ContentInvalidError):
        adapter("openai", handler).plan(MESSAGES, TOOLS)


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_answer_stream_cancellation_closes_the_sdk_http_stream(provider):
    class Body(httpx.SyncByteStream):
        closed = False
        def __iter__(self):
            yield stream_response(provider, "stop" if provider == "openai" else "end_turn").content
        def close(self):
            self.closed = True
    body = Body()
    llm = adapter(provider, lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body))
    events = llm.answer_stream(MESSAGES, thinking_budget=0)
    assert next(events) == ("thinking", "consider")
    events.close()
    assert body.closed is True
