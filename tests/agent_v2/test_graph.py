import json

from agent_core.runner import canonical_tool_args, run_agent
from tests.agent_v2.agent_fakes import (
    FakeLlm,
    FakeRepositories,
    make_chunk,
    make_dependencies,
    make_recall,
    make_request,
    make_vault_item,
)


def _run(llm, repos, *, question="查一下我的记录", intent=None, run_id="run-1"):
    if intent is not None:
        llm.intent = intent
    return list(run_agent(make_request(run_id=run_id, question=question), make_dependencies(llm, repos)))


def _terminal(events):
    terminals = [event for event in events if event.kind in {"completed", "failed"}]
    assert len(terminals) == 1
    return terminals[0]


def _read_intent(**overrides):
    value = {
        "action_kind": "read",
        "route": "semantic",
        "standalone_question": "查一下我的记录",
        "content_type": None,
        "moods": [],
        "time_expression": {"kind": "none"},
        "file_query_mode": "none",
    }
    value.update(overrides)
    return value


def test_done_true_with_calls_executes_the_final_batch_then_completes():
    llm = FakeLlm(plans=['{"calls":[{"tool":"search_records","args":{"query":"镜子"}}],"done":true}'])
    repos = FakeRepositories([make_chunk()])

    events = _run(llm, repos)

    terminal = _terminal(events)
    assert terminal.kind == "completed"
    assert terminal.metadata["stop_reason"] == "done"
    assert terminal.metadata["plan_rounds"] == 1
    assert terminal.metadata["tool_call_count"] == 1
    assert len(repos.records.search_calls) == 1
    assert terminal.metadata["sources"][0].record_id == 10


def test_empty_plan_stops_normally_and_preserves_no_material_fallback():
    llm = FakeLlm(plans=['{"calls":[],"done":false}'])
    repos = FakeRepositories()

    events = _run(llm, repos)

    terminal = _terminal(events)
    assert terminal.kind == "completed"
    assert terminal.metadata["stop_reason"] == "empty_calls"
    assert terminal.metadata["fallback"] is True
    assert terminal.metadata["tool_call_count"] == 0
    assert "已授权记录" in terminal.metadata["answer_text"]


def test_fourth_planning_round_executes_its_calls_but_never_starts_a_fifth():
    plans = [
        json.dumps({"calls": [{"tool": "search_records", "args": {"query": f"第{i}"}}], "done": False})
        for i in range(4)
    ]
    llm = FakeLlm(plans=plans)
    repos = FakeRepositories([make_chunk()])

    events = _run(llm, repos)

    terminal = _terminal(events)
    assert terminal.kind == "completed"
    assert terminal.metadata["stop_reason"] == "max_rounds"
    assert terminal.metadata["plan_rounds"] == 4
    assert terminal.metadata["tool_call_count"] == 4
    assert llm.plan_calls == 4


def test_duplicate_json_args_are_normalized_before_repeat_detection():
    plans = [
        '{"calls":[{"tool":"search_records","args":{"query":"镜子","limit":5}}],"done":false}',
        '{"calls":[{"tool":"search_records","args":{"limit":5,"query":"镜子"}}],"done":false}',
    ]
    llm = FakeLlm(plans=plans)
    repos = FakeRepositories([make_chunk()])

    terminal = _terminal(_run(llm, repos))

    assert terminal.kind == "completed"
    assert terminal.metadata["stop_reason"] == "duplicate_call"
    assert terminal.metadata["plan_rounds"] == 2
    assert terminal.metadata["tool_call_count"] == 1
    assert len(repos.records.search_calls) == 1
    assert canonical_tool_args({"nested": {"b": 2, "a": 1}}) == canonical_tool_args(
        {"nested": {"a": 1, "b": 2}}
    )


def test_malformed_or_missing_planner_final_keeps_initial_retrieval_evidence():
    llm = FakeLlm(plans=["{\"calls\":["] , answer="记录里有这条事实[1]。")
    repos = FakeRepositories([make_chunk()])

    terminal = _terminal(_run(llm, repos))

    assert terminal.kind == "completed"
    assert terminal.metadata["stop_reason"] == "planner_error"
    assert terminal.metadata["sources"][0].record_id == 10
    assert terminal.metadata["answer_text"] == "记录里有这条事实[1]。"


def test_requested_file_body_gets_one_narrow_auto_backstop_read():
    item = make_vault_item()
    llm = FakeLlm(
        intent=_read_intent(route="hybrid", file_query_mode="body"),
        plans=[
            '{"calls":[{"tool":"find_item","args":{"query":"设计稿"}}],"done":false}',
            '{"calls":[],"done":true}',
        ],
        answer="设计稿说明了边界[F1]。",
    )
    repos = FakeRepositories(items=[item], recall=make_recall(item))

    events = _run(llm, repos, intent=llm.intent)

    terminal = _terminal(events)
    assert terminal.kind == "completed"
    assert terminal.metadata["stop_reason"] == "empty_calls"
    assert [call[1] for call in repos.vault.recall_calls] == [item.id]
    tool_events = [event for event in events if event.kind == "tool_finished"]
    assert len(tool_events) == 2
    assert tool_events[-1].metadata["auto_backstop"] is True
    assert terminal.metadata["vault_refs"][0].vault_item_id == item.id


def test_failed_file_lookup_does_not_run_the_auto_backstop():
    llm = FakeLlm(
        intent=_read_intent(route="hybrid", file_query_mode="body"),
        plans=['{"calls":[{"tool":"find_item","args":{"query":"设计稿"}}],"done":false}'],
    )
    repos = FakeRepositories(fail_find=True)

    events = _run(llm, repos, intent=llm.intent)

    terminal = _terminal(events)
    assert terminal.kind == "failed"
    assert terminal.metadata["stop_reason"] == "read_error"
    assert repos.vault.find_calls
    assert repos.vault.recall_calls == []


def test_two_invocations_on_one_compiled_graph_start_with_fresh_state():
    llm = FakeLlm(plans=['{"calls":[],"done":true}', '{"calls":[],"done":true}'])
    repos = FakeRepositories([make_chunk()])
    dependencies = make_dependencies(llm, repos)

    first = list(run_agent(make_request(run_id="run-first", question="first"), dependencies))
    second = list(run_agent(make_request(run_id="run-second", question="second"), dependencies))

    first_terminal = _terminal(first)
    second_terminal = _terminal(second)
    assert first_terminal.metadata["plan_rounds"] == second_terminal.metadata["plan_rounds"] == 1
    assert [event.metadata["route"] for event in first if event.kind == "meta"] == ["semantic"]
    assert [event.metadata["route"] for event in second if event.kind == "meta"] == ["semantic"]
    assert llm.plan_calls == 2
