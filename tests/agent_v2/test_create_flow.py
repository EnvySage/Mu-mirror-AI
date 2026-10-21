from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID, uuid5

import pytest

from agent_core.nodes import create_record
from agent_core.runner import make_runtime, run_agent
from agent_core.types import AgentState, CreatedRecord
from agent_tools.command_client import CommandResult, CREATE_RECORD_SLOT
from agent_tools.registry import READ_TOOL_REGISTRY, get_read_tool_specs
from tests.agent_v2.agent_fakes import FakeLlm, FakeRepositories, make_dependencies, make_request


RUN_ID = "a03c2b8c-fbf2-4a64-8ed9-6b7d1ac77a10"
CONTENT = "今天把镜子的聊天功能跑通了"
QUESTION = f"帮我记一篇日记：{CONTENT}"
CALL_ID = str(uuid5(UUID(RUN_ID), CREATE_RECORD_SLOT))


class RecordingCommandClient:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def create_record(self, runtime, content):
        self.calls.append((runtime.run_id, content))
        return self.result


def _authorized_request(*, run_id=RUN_ID):
    request = make_request(run_id=run_id, question=QUESTION)
    request.command_grant = "java-issued-test-grant"
    request.authorized_record_content = CONTENT
    request.allowed_write_tools.append("create_record")
    return request


def _run_with_result(result, *, run_id=RUN_ID):
    llm = FakeLlm()
    repositories = FakeRepositories()
    client = RecordingCommandClient(result)
    embedder_factory_calls = []

    def forbidden_embedder_factory(**kwargs):
        embedder_factory_calls.append(kwargs)
        raise AssertionError("authorized record creation must not initialize embeddings")

    dependencies = replace(
        make_dependencies(llm, repositories),
        embedding_factory=forbidden_embedder_factory,
        command_client=client,
    )
    events = list(run_agent(_authorized_request(run_id=run_id), dependencies))
    return events, llm, repositories, client, embedder_factory_calls


def _terminal(events):
    terminals = [event for event in events if event.kind in {"completed", "failed"}]
    assert len(terminals) == 1
    return terminals[0]


def test_explicit_create_uses_the_java_original_without_rag_or_model_rewriting():
    result = CommandResult(
        "success", "OK", CALL_ID, 731, "record_only", "2026-10-20T12:00:00+08:00",
    )

    events, llm, repositories, client, embedder_factory_calls = _run_with_result(result)

    terminal = _terminal(events)
    assert terminal.kind == "completed"
    assert terminal.metadata["outcome"] == "action_success"
    assert terminal.metadata["stop_reason"] == "write_completed"
    assert terminal.metadata["answer_text"] == "已保存这条日记；目前是仅记录状态"
    assert terminal.metadata["sources"] == []
    assert terminal.metadata["created_records"] == [
        CreatedRecord(731, "record_only", "2026-10-20T12:00:00+08:00")
    ]
    assert [event.content for event in events if event.kind == "delta"] == [
        "已保存这条日记；目前是仅记录状态"
    ]
    assert client.calls == [(RUN_ID, CONTENT)]
    assert repositories.records.search_calls == []
    assert repositories.records.vector_calls == []
    assert embedder_factory_calls == []
    assert not hasattr(llm, "intent_kwargs")
    assert llm.plan_calls == 0
    assert llm.answer_calls == 0
    tool_events = [event for event in events if event.kind == "tool_finished"]
    assert len(tool_events) == 1
    assert CONTENT not in tool_events[0].metadata["args_json"]


def test_unknown_command_result_stays_unknown_without_success_delta_or_receipt():
    result = CommandResult("unknown", "RECEIPT_NOT_CONFIRMED", CALL_ID)

    events, llm, repositories, client, _ = _run_with_result(result)

    terminal = _terminal(events)
    assert terminal.kind == "completed"
    assert terminal.metadata["outcome"] == "action_unknown"
    assert terminal.metadata["stop_reason"] == "write_unknown"
    assert terminal.metadata["created_records"] == []
    assert [event for event in events if event.kind == "delta"] == []
    assert client.calls == [(RUN_ID, CONTENT)]
    assert repositories.records.search_calls == []
    assert repositories.records.vector_calls == []
    assert llm.plan_calls == 0
    assert llm.answer_calls == 0


def test_explicit_command_rejection_is_a_failed_terminal_not_a_success():
    events, _, _, _, _ = _run_with_result(CommandResult("failed", "CONTENT_TOO_LONG", CALL_ID))

    terminal = _terminal(events)
    assert terminal.kind == "failed"
    assert terminal.metadata["stop_reason"] == "write_failed"
    assert terminal.metadata["code"] == "CONTENT_TOO_LONG"
    assert [event for event in events if event.kind == "delta"] == []


def test_create_record_is_not_a_model_tool_and_node_rejects_missing_authority():
    assert "create_record" not in READ_TOOL_REGISTRY
    assert "create_record" not in {item["name"] for item in get_read_tool_specs()}

    llm = FakeLlm()
    repositories = FakeRepositories()
    client = RecordingCommandClient(CommandResult("success", "OK", CALL_ID, 731, "record_only"))
    request = make_request(run_id=RUN_ID, question=QUESTION, embedding=False)
    dependencies = replace(make_dependencies(llm, repositories, embedding=False), command_client=client)
    context = make_runtime(request, dependencies)

    result = create_record(
        AgentState(question=QUESTION, action_kind="create_record"),
        SimpleNamespace(context=context),
    )

    assert result["outcome"] == "action_failed"
    assert result["error_code"] == "WRITE_NOT_ENABLED"
    assert client.calls == []


def test_shadow_run_rejects_even_a_well_formed_write_grant():
    request = _authorized_request()
    request.shadow = True

    with pytest.raises(ValueError, match="shadow runs cannot carry a write grant"):
        make_runtime(request, make_dependencies(FakeLlm(), FakeRepositories()))
