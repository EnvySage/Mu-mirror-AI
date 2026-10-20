from agent_core.runner import run_agent
from tests.agent_v2.agent_fakes import (
    FakeClock,
    FakeLlm,
    FakeRepositories,
    make_chunk,
    make_dependencies,
    make_request,
)


def _terminal(events):
    values = [event for event in events if event.kind in {"completed", "failed"}]
    assert len(values) == 1
    return values[0]


def test_cancel_during_planner_stream_closes_stream_and_starts_no_tool():
    cancelled = {"value": False}
    llm = FakeLlm(
        plans=['{"calls":[{"tool":"search_records","args":{}}],"done":true}'],
        on_plan_yield=lambda: cancelled.__setitem__("value", True),
    )
    repos = FakeRepositories([make_chunk()])

    events = list(run_agent(
        make_request(),
        make_dependencies(llm, repos),
        cancellation=lambda: cancelled["value"],
    ))

    terminal = _terminal(events)
    assert terminal.kind == "failed"
    assert terminal.metadata["stop_reason"] == "cancelled"
    assert repos.records.search_calls == []
    assert llm.closed_streams == 1


def test_loop_budget_decreases_across_rounds_and_still_answers_existing_evidence():
    clock = FakeClock()
    plans = [
        '{"calls":[{"tool":"search_records","args":{"query":"镜子"}}],"done":false}',
        '{"calls":[{"tool":"search_records","args":{"query":"项目"}}],"done":false}',
        '{"calls":[],"done":true}',
    ]
    llm = FakeLlm(plans=plans, clock=clock, plan_advance=90)
    repos = FakeRepositories([make_chunk()])
    dependencies = make_dependencies(llm, repos, clock=clock)

    events = list(run_agent(make_request(), dependencies))

    terminal = _terminal(events)
    assert terminal.kind == "completed"
    assert terminal.metadata["stop_reason"] == "loop_budget"
    assert terminal.metadata["plan_rounds"] == 2
    assert terminal.metadata["tool_call_count"] == 1
    assert llm.plan_calls == 2
    assert terminal.metadata["sources"][0].record_id == 10


def test_total_deadline_is_separate_from_loop_budget_and_fails_closed():
    clock = FakeClock()
    llm = FakeLlm(
        plans=['{"calls":[{"tool":"search_records","args":{}}],"done":false}'],
        clock=clock,
        plan_advance=481,
    )
    repos = FakeRepositories([make_chunk()])
    dependencies = make_dependencies(llm, repos, clock=clock)

    terminal = _terminal(list(run_agent(make_request(), dependencies)))

    assert terminal.kind == "failed"
    assert terminal.metadata["stop_reason"] == "total_deadline"
    assert repos.records.search_calls == []
    assert llm.plan_calls == 1


def test_runtime_resources_close_when_graph_finishes():
    llm = FakeLlm(plans=['{"calls":[],"done":true}'])
    repos = FakeRepositories([make_chunk()])
    dependencies = make_dependencies(llm, repos)
    request = make_request()

    events = list(run_agent(request, dependencies))

    assert _terminal(events).kind == "completed"
    assert llm.closed is True
