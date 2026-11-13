"""Explicit request usage survives graph worker contexts, without global state."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import Context
import json

import httpx

from agent_core.graph import build_graph
from agent_core.runner import make_runtime
from agent_core.types import AgentState
from tests.agent_v2.agent_fakes import FakeRepositories, make_dependencies, make_request
from tests.test_ai_usage import MESSAGES, adapter, chunk, collect, completion, sse


def test_graph_rebinds_request_collector_in_fresh_worker_context():
    def handler(request):
        body = json.loads(request.content)
        if body.get("stream"):
            return sse(chunk(text="你好。"), chunk(finish="stop"), chunk(usage={"total_tokens": 5}), "[DONE]")
        response = completion({"total_tokens": 3})
        response["choices"][0]["message"]["content"] = json.dumps({
            "action_kind": "conversation", "route": "hybrid", "standalone_question": "hello",
            "content_type": None, "moods": [], "time_expression": {"kind": "none"}, "file_query_mode": "none"})
        return httpx.Response(200, json=response)
    llm = adapter("openai", handler)
    with collect() as collector:
        runtime = make_runtime(make_request(embedding=False), make_dependencies(llm, FakeRepositories()))
        graph = build_graph()
        def run():
            return list(graph.stream(AgentState(question="hello"), context=runtime, stream_mode="custom"))
        with ThreadPoolExecutor(max_workers=1) as pool:
            events = pool.submit(Context().run, run).result(timeout=10)
    assert any(event["kind"] == "completed" for event in events)
    receipts = json.loads(collector.to_bytes())["attempts"]
    assert [r["total_tokens"] for r in receipts] == [3, 5]
