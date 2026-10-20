import os
import threading
import time
from types import SimpleNamespace

import grpc
import pytest

from agent_core.runner import AgentDependencies
from agent_data.glossary import GlossaryRepository
from agent_data.history import HistoryRepository
from agent_data.pool import create_read_pool
from agent_data.profiles import ProfileRepository
from agent_data.records import RecordRepository
from agent_data.todos import TodoRepository
from agent_data.vault import VaultRepository
from generated import agent_pb2, agent_pb2_grpc
from server import build_agent_server, build_legacy_server
from services.agent_service import AgentRunnerServicer
from tests.agent_v2.agent_fakes import FakeLlm, FakeRepositories, make_dependencies, make_request


TOKEN = "test-agent-internal-token"


def _run_channel(request, port, *, token=TOKEN):
    with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
        stub = agent_pb2_grpc.AgentRunnerStub(channel)
        metadata = (("x-internal-token", token),) if token else ()
        return list(stub.RunAgent(request, metadata=metadata, timeout=5))


def test_missing_or_wrong_internal_token_is_rejected_before_dependency_factory():
    llm = FakeLlm(intent={
        "action_kind": "conversation", "route": "hybrid", "standalone_question": "hello",
        "content_type": None, "moods": [], "time_expression": {"kind": "none"}, "file_query_mode": "none",
    })
    repositories = FakeRepositories()
    count = {"created": 0}

    def factory():
        count["created"] += 1
        return make_dependencies(llm, repositories)

    server, _, port = build_agent_server(
        port=0,
        servicer=AgentRunnerServicer(internal_token=TOKEN, dependencies_factory=factory),
    )
    server.start()
    try:
        with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = agent_pb2_grpc.AgentRunnerStub(channel)
            for metadata in ((), (("x-internal-token", "wrong"),)):
                with pytest.raises(grpc.RpcError) as error:
                    list(stub.RunAgent(make_request(), metadata=metadata, timeout=3))
                assert error.value.code() == grpc.StatusCode.UNAUTHENTICATED
        assert count["created"] == 0
        assert repositories.glossary.calls == []
        assert llm.plan_calls == 0
    finally:
        server.stop(0)


def test_authenticated_grpc_events_use_run_id_monotonic_seq_and_one_terminal():
    llm = FakeLlm(
        intent={
            "action_kind": "conversation", "route": "hybrid", "standalone_question": "hello",
            "content_type": None, "moods": [], "time_expression": {"kind": "none"}, "file_query_mode": "none",
        },
        answer="你好。",
    )
    repos = FakeRepositories()
    servicer = AgentRunnerServicer(internal_token=TOKEN, dependencies=make_dependencies(llm, repos))
    server, _, port = build_agent_server(port=0, servicer=servicer)
    server.start()
    try:
        events = _run_channel(make_request(run_id="grpc-run-9", question="你好"), port)
    finally:
        server.stop(0)
        servicer.close()

    assert events
    assert [event.seq for event in events] == list(range(1, len(events) + 1))
    assert {event.run_id for event in events} == {"grpc-run-9"}
    assert [event.WhichOneof("event") for event in events].count("completed") == 1
    assert events[-1].completed.answer_text == "你好。"


def test_agent_server_keeps_a_hard_two_rpc_limit():
    started = 0
    lock = threading.Lock()
    both_started = threading.Event()
    release = threading.Event()

    class BlockingGlossary:
        def top_confirmed(self, scope, *, limit):
            nonlocal started
            with lock:
                started += 1
                if started >= 2:
                    both_started.set()
            release.wait(5)
            return []

    repositories = FakeRepositories()
    repositories.glossary = BlockingGlossary()
    llm = FakeLlm(intent={
        "action_kind": "conversation", "route": "hybrid", "standalone_question": "hello",
        "content_type": None, "moods": [], "time_expression": {"kind": "none"}, "file_query_mode": "none",
    })
    servicer = AgentRunnerServicer(internal_token=TOKEN, dependencies=make_dependencies(llm, repositories))
    server, _, port = build_agent_server(port=0, servicer=servicer)
    server.start()
    errors = []

    def consume(request):
        try:
            _run_channel(request, port)
        except Exception as exc:  # keep worker exceptions visible after join
            errors.append(exc)

    threads = [
        threading.Thread(target=consume, args=(make_request(run_id=f"blocked-{i}"),), daemon=True)
        for i in range(2)
    ]
    try:
        for thread in threads:
            thread.start()
        assert both_started.wait(3), "the first two RPCs did not occupy both Agent workers"
        with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = agent_pb2_grpc.AgentRunnerStub(channel)
            call = stub.RunAgent(make_request(run_id="over-limit"), metadata=(("x-internal-token", TOKEN),), timeout=3)
            with pytest.raises(grpc.RpcError) as error:
                next(call)
            assert error.value.code() == grpc.StatusCode.RESOURCE_EXHAUSTED
    finally:
        release.set()
        for thread in threads:
            thread.join(5)
        server.stop(0)
        servicer.close()
    assert errors == []


def test_legacy_server_still_registers_the_existing_10003_services():
    server, _ = build_legacy_server(port=0)
    try:
        names = {handler.service_name() for handler in server._state.generic_handlers}
        assert {"mirror.MirrorChat", "mirror.RecordProcessor", "mirror.EmbeddingService", "mirror.MirrorProfile"} <= names
    finally:
        server.stop(0)


def test_authenticated_agent_reads_isolated_pg_directly_without_java_clients():
    dsn = os.getenv("MIRROR_TEST_READ_DSN", "")
    if not dsn:
        pytest.skip("MIRROR_TEST_READ_DSN is required for the isolated PostgreSQL integration check")

    class PgEmbedder:
        def embed(self, text, **kwargs):
            return [1.0, 1.0] + [0.0] * 1022

    class PgLlm(FakeLlm):
        def __init__(self):
            super().__init__(
                intent={
                    "action_kind": "read", "route": "semantic", "standalone_question": "approved coverage phrase",
                    "content_type": None, "moods": [], "time_expression": {"kind": "none"}, "file_query_mode": "none",
                },
                plans=['{"calls":[{"tool":"get_stats","args":{}}],"done":true}'],
                answer="隔离库只返回已授权统计。",
            )

        def close(self):
            pass

    pool = create_read_pool(dsn)
    repos = SimpleNamespace(
        pool=pool,
        records=RecordRepository(pool),
        todos=TodoRepository(pool),
        profiles=ProfileRepository(pool),
        glossary=GlossaryRepository(pool),
        history=HistoryRepository(pool),
        vault=VaultRepository(pool),
    )
    llm = PgLlm()
    dependencies = AgentDependencies(
        repositories=repos,
        llm_factory=lambda **kwargs: llm,
        embedding_factory=lambda **kwargs: PgEmbedder(),
    )
    servicer = AgentRunnerServicer(internal_token=TOKEN, dependencies=dependencies)
    server, _, port = build_agent_server(port=0, servicer=servicer)
    server.start()
    try:
        request = make_request(run_id="pg-agent-run", question="统计已授权记录")
        request.user_message_id = 904
        events = _run_channel(request, port)
    finally:
        server.stop(0)
        servicer.close()

    assert events[-1].run_id == "pg-agent-run"
    assert events[-1].WhichOneof("event") == "completed"
    assert events[-1].completed.tool_call_count == 1
    assert events[-1].completed.outcome == "answer"
    # AgentDependencies intentionally has repositories and direct SDK clients only; no Java RPC/HTTP client.
    assert dependencies.repositories is repos
    assert not hasattr(dependencies, "command_client")
