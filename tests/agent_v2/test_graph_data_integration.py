"""Actual SDK -> compiled graph -> scoped PostgreSQL -> streamed gRPC completion."""
import json
from types import SimpleNamespace

from agent_core.runner import AgentDependencies, run_agent
from agent_data.glossary import GlossaryRepository
from agent_data.history import HistoryRepository
from agent_data.profiles import ProfileRepository
from agent_data.records import RecordRepository
from agent_data.todos import TodoRepository
from agent_data.vault import VaultRepository
from services.agent_service import AgentRunnerServicer
from tests.agent_v2.agent_fakes import FakeEmbedder, make_request
from tests.test_graph_planning_protocol import adapter


def test_native_model_plan_reads_real_authorized_file_and_emits_accepted_completion(agent_pool):
    import httpx
    plans = []
    def reply(content, tool=None):
        message = {"role": "assistant", "content": content}
        if tool:
            message["tool_calls"] = [{"id": "call_file", "type": "function", "function": {
                "name": "find_item", "arguments": '{"query":"specification.pdf"}'}}]
        return httpx.Response(200, json={"id": "c1", "object": "chat.completion", "created": 1, "model": "test",
            "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if tool else "stop"}]})

    def transport(request):
        body = json.loads(request.content)
        if body.get("stream"):
            prompt = body["messages"][0]["content"]
            assert "approved vault body segment alpha" in prompt
            assert "approved vault body segment beta" in prompt
            assert "other user confirmed body" not in prompt
            assert "entire file source must not be used" not in prompt
            events = [dict(id="c1", object="chat.completion.chunk", created=1, model="test", choices=[dict(
                index=0, delta={"content": "文件分别讨论了 alpha 与 beta，两段可以结合分析[F1]。"}, finish_reason=None)]),
                dict(id="c1", object="chat.completion.chunk", created=1, model="test", choices=[dict(
                    index=0, delta={}, finish_reason="stop")])]
            stream = "".join("data: " + json.dumps(event, ensure_ascii=False) + "\n\n" for event in events) + "data: [DONE]\n\n"
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream.encode())
        if "tools" in body:
            plans.append(body)
            return reply(None, tool=True) if len(plans) == 1 else reply("已有文件候选，结束规划。")
        return reply(json.dumps(dict(action_kind="read", route="hybrid", standalone_question="分析 specification.pdf 全文",
            content_type=None, moods=[], time_expression={"kind": "none"}, file_query_mode="body")))

    llm = adapter("openai", transport)
    repos = SimpleNamespace(records=RecordRepository(agent_pool), todos=TodoRepository(agent_pool),
        vault=VaultRepository(agent_pool), glossary=GlossaryRepository(agent_pool),
        history=HistoryRepository(agent_pool), profiles=ProfileRepository(agent_pool))
    dependencies = AgentDependencies(repositories=repos, llm_factory=lambda **kwargs: llm,
                                     embedding_factory=lambda **kwargs: FakeEmbedder())
    events = list(run_agent(make_request(question="分析 specification.pdf 全文"), dependencies))
    completed, = [event for event in events if event.kind == "completed"]
    assert not any(event.kind == "failed" for event in events)
    assert completed.metadata["outcome"] == "answer"
    assert completed.metadata["vault_refs"][0].vault_item_id == 303
    assert completed.metadata["metrics"]["diagnostics"]["planning_protocol"] == "native"
    assert completed.metadata["metrics"]["diagnostics"]["answer_finish_reason"] == "stop"
    service = AgentRunnerServicer(internal_token="test-only", dependencies=dependencies)
    proto = service._to_proto_event("run-1", completed, len(events), {})
    assert proto.completed.answer_text == completed.metadata["answer_text"]
    assert proto.completed.metrics.prompt_hashes["agent_chat"]
