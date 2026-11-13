"""Real gRPC trailer contracts, including errors, empty calls and embedding exclusion."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
from uuid import uuid4

import grpc
import httpx
import pytest

from tests.test_ai_usage import adapter, completion, MESSAGES, usage_module


def metadata(invocation=None):
    return (("mirror-usage-invocation-id", invocation or str(uuid4())),
            ("mirror-usage-task-id", str(uuid4())), ("mirror-usage-scene", "model_test"))


@contextmanager
def server(method, handler, *, streaming=False):
    usage = usage_module()
    assert hasattr(usage, "UsageServerInterceptor"), "gRPC usage trailer interceptor is missing"
    rpc = grpc.unary_stream_rpc_method_handler(handler) if streaming else grpc.unary_unary_rpc_method_handler(handler)
    service, name = method.rsplit("/", 1)
    instance = grpc.server(ThreadPoolExecutor(max_workers=2), interceptors=[usage.UsageServerInterceptor()])
    instance.add_generic_rpc_handlers([grpc.method_handlers_generic_handler(service, {name: rpc})])
    port = instance.add_insecure_port("127.0.0.1:0")
    instance.start()
    try:
        with grpc.insecure_channel(f"127.0.0.1:{port}", options=[("grpc.max_metadata_size", 65536)]) as channel:
            yield channel.unary_stream("/" + method) if streaming else channel.unary_unary("/" + method)
    finally:
        instance.stop(0).wait()


def report(call):
    trailers = dict(call.trailing_metadata())
    assert "mirror-usage-bin" in trailers
    return json.loads(trailers["mirror-usage-bin"])


def test_unary_reports_usage_and_preserves_other_trailers():
    llm = adapter("openai", lambda _: httpx.Response(200, json=completion({"total_tokens": 50})))
    def handler(request, context):
        context.set_trailing_metadata((("other", "preserved"),))
        return llm.chat(MESSAGES).encode()
    headers = metadata()
    with server("test.Llm/Run", handler) as rpc:
        result, call = rpc.with_call(b"request", metadata=headers)
    assert result == b"private answer"
    observed = report(call)
    assert observed["invocation_id"] == headers[0][1]
    assert observed["attempts"][0]["total_tokens"] == 50
    assert dict(call.trailing_metadata())["other"] == "preserved"


@pytest.mark.parametrize("streaming", [False, True])
def test_error_after_upstream_call_still_reports_known_usage(streaming):
    llm = adapter("openai", lambda _: httpx.Response(200, json=completion({"total_tokens": 7})))
    def unary(request, context):
        llm.chat(MESSAGES)
        context.abort(grpc.StatusCode.INVALID_ARGUMENT, "business failure")
    def stream(request, context):
        yield b"prefix"
        unary(request, context)
    with server("test.Llm/Run", stream if streaming else unary, streaming=streaming) as rpc:
        with pytest.raises(grpc.RpcError) as error:
            if streaming:
                list(rpc(b"request", metadata=metadata()))
            else:
                rpc(b"request", metadata=metadata())
    assert error.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    assert report(error.value)["attempts"][0]["total_tokens"] == 7


def test_completed_stream_reports_after_iterator_finally():
    llm = adapter("openai", lambda _: httpx.Response(200, json=completion({"total_tokens": 3})))
    def handler(request, context):
        try:
            yield b"answer"
        finally:
            llm.chat(MESSAGES)
    with server("test.Llm/Run", handler, streaming=True) as rpc:
        call = rpc(b"request", metadata=metadata())
        assert list(call) == [b"answer"]
    assert report(call)["attempts"][0]["total_tokens"] == 3


def test_no_upstream_request_is_complete_empty_report():
    with server("test.Llm/Run", lambda r, c: b"local") as rpc:
        _, call = rpc.with_call(b"request", metadata=metadata())
    observed = report(call)
    assert observed["attempts"] == []
    assert observed["complete"] is True


@pytest.mark.parametrize("method,headers", [
    ("test.Llm/Run", ()), ("test.Llm/Run", (("mirror-usage-invocation-id", "invalid"),)),
    ("mirror.EmbeddingService/Embed", metadata()), ("mirror.EmbeddingService/GetModelInfo", metadata())])
def test_old_callers_and_embedding_never_collect(method, headers):
    llm = adapter("openai", lambda _: httpx.Response(200, json=completion({"total_tokens": 100})))
    with server(method, lambda r, c: llm.chat(MESSAGES).encode()) as rpc:
        _, call = rpc.with_call(b"request", metadata=headers)
    assert "mirror-usage-bin" not in dict(call.trailing_metadata())


def test_request_after_previous_rpc_does_not_inherit_collection():
    with server("test.Llm/Run", lambda r, c: b"ok") as rpc:
        _, first = rpc.with_call(b"request", metadata=metadata())
        _, second = rpc.with_call(b"request")
    assert report(first)["complete"] is True
    assert "mirror-usage-bin" not in dict(second.trailing_metadata())


def test_legacy_builder_registers_interceptor_without_changing_embedding_health():
    from server import build_legacy_server
    from generated import embedding_pb2 as embedding, embedding_pb2_grpc
    from generated import record_processor_pb2 as records, record_processor_pb2_grpc
    instance, port = build_legacy_server(port=0)
    instance.start()
    try:
        with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = embedding_pb2_grpc.EmbeddingServiceStub(channel)
            _, call = stub.GetModelInfo.with_call(embedding.ModelInfoRequest(), metadata=metadata())
            assert "mirror-usage-bin" not in dict(call.trailing_metadata())
            with pytest.raises(grpc.RpcError) as error:
                record_processor_pb2_grpc.RecordProcessorStub(channel).Classify(records.ClassifyRequest(), metadata=metadata())
            assert report(error.value)["attempts"] == []
    finally:
        instance.stop(0).wait()


def test_agent_builder_reports_complete_empty_for_unauthorized_call():
    from server import build_agent_server
    from services.agent_service import AgentRunnerServicer
    from generated import agent_pb2, agent_pb2_grpc
    service = AgentRunnerServicer(internal_token="test-only")
    instance, _, port = build_agent_server(port=0, servicer=service)
    instance.start()
    try:
        with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
            with pytest.raises(grpc.RpcError) as error:
                list(agent_pb2_grpc.AgentRunnerStub(channel).RunAgent(agent_pb2.AgentRunRequest(), metadata=metadata()))
            assert error.value.code() == grpc.StatusCode.UNAUTHENTICATED
            assert report(error.value)["attempts"] == []
            assert report(error.value)["complete"] is True
    finally:
        instance.stop(0).wait()
        service.close()


def test_report_near_48kib_is_delivered_with_prefix_and_incomplete_coverage():
    llm = adapter("openai", lambda _: httpx.Response(200, json=completion({
        "total_tokens": 1, "credits_consumed": "0.123456789012", "credit_unit": "积" * 32},
        model="qwen-" + "x" * 250)))
    def handler(request, context):
        for _ in range(100):
            llm.chat(MESSAGES)
        return b"ok"
    with server("test.Llm/Run", handler) as rpc:
        result, call = rpc.with_call(b"request", metadata=metadata(), timeout=10)
    assert result == b"ok"
    payload = dict(call.trailing_metadata())["mirror-usage-bin"]
    assert 40 * 1024 < len(payload) <= 48 * 1024
    observed = json.loads(payload)
    assert observed["complete"] is False
    assert 0 < len(observed["attempts"]) < 100
