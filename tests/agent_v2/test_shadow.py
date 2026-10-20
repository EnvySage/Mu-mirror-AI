from __future__ import annotations

from dataclasses import replace

import pytest

from agent_core.runner import make_runtime
from eval.agent_v2.replay import replay, simulated_result
from eval.agent_v2.report import build_report, read_jsonl
from tests.agent_v2.agent_fakes import FakeLlm, FakeRepositories, make_dependencies, make_request


def test_evaluation_suite_has_at_least_40_permission_and_failure_cases():
    cases = read_jsonl("eval/agent_v2/cases.jsonl")

    assert len(cases) >= 40
    assert {"permissions", "failures", "writes", "dates", "files"}.issubset(
        {case["category"] for case in cases}
    )
    assert all(case["reference_date"] == "2026-10-20" for case in cases)
    assert all(case["model_config_ref"] and "api_key" not in case for case in cases)


def test_simulated_replay_marks_every_result_and_never_claims_a_write_or_quality_result():
    cases = read_jsonl("eval/agent_v2/cases.jsonl")
    runs = replay(cases, simulated_result)
    report = build_report(cases, runs)

    assert report["execution_label"] == "SIMULATED_HARNESS_ONLY"
    assert report["case_count"] >= 40
    assert report["run_count"] == report["case_count"] * 2
    assert report["complete_pair_count"] == report["case_count"]
    assert report["quality_claim"] == "none_simulated"
    assert all(row["simulated"] for row in runs)
    assert all(row["stop_reason"] == "SIMULATED" for row in runs)
    assert all(row["business_write_success"] is False for row in runs)
    assert all(row["manual_score"] is None for row in runs)
    assert all(row["tokens"]["known"] is False for row in runs)


@pytest.mark.parametrize(("field", "wrong_value"), [
    ("case_id", "another-case"),
    ("engine", "graph_v2"),
    ("fixture_id", "another-fixture"),
    ("reference_date", "2026-10-21"),
    ("model_config_ref", "another-model"),
    ("prompt_profile", "another-prompt"),
])
def test_replay_rejects_adapter_pairing_metadata_mismatch(field, wrong_value):
    case = read_jsonl("eval/agent_v2/cases.jsonl")[0]

    def mismatched_adapter(case, engine):
        result = simulated_result(case, engine)
        result[field] = wrong_value
        return result

    with pytest.raises(ValueError, match="adapter pairing metadata mismatch"):
        replay([case], mismatched_adapter)


def test_replay_rejects_adapter_missing_pairing_metadata():
    case = read_jsonl("eval/agent_v2/cases.jsonl")[0]

    def incomplete_adapter(case, engine):
        result = simulated_result(case, engine)
        del result["prompt_profile"]
        return result

    with pytest.raises(ValueError, match="adapter pairing metadata mismatch.*prompt_profile"):
        replay([case], incomplete_adapter)


def test_report_rejects_mismatched_pair_metadata_when_built_from_external_rows():
    case = read_jsonl("eval/agent_v2/cases.jsonl")[0]
    runs = replay([case], simulated_result)
    runs[1]["model_config_ref"] = "another-model"

    with pytest.raises(ValueError, match="run pairing metadata mismatch.*model_config_ref"):
        build_report([case], runs)


def test_shadow_runtime_has_no_write_capability_and_rejects_any_write_grant():
    request = make_request(shadow=True, question="只读影子评测问题")
    dependencies = make_dependencies(FakeLlm(), FakeRepositories())
    dependencies = replace(dependencies, command_client=object())

    runtime = make_runtime(request, dependencies)

    assert runtime.shadow is True
    assert runtime.allowed_write_tools == ()
    assert runtime.command_client is None
    assert runtime.command_grant is None
    runtime.llm.close()
    runtime.embedding_client.close()

    request.command_grant = "opaque-token"
    request.authorized_record_content = "未经允许的正文"
    request.allowed_write_tools.append("create_record")
    with pytest.raises(ValueError, match="shadow runs cannot carry a write grant"):
        make_runtime(request, dependencies)


def test_legacy_server_starts_without_agent_dsn_when_agent_is_disabled(monkeypatch):
    import server

    class FakeServer:
        started = False
        stopped = False

        def start(self):
            self.started = True

        def stop(self, _grace):
            self.stopped = True

    legacy = FakeServer()
    monkeypatch.setitem(server.CONFIG["agent"], "enabled", False)
    monkeypatch.delenv("MIRROR_AI_AGENT_ENABLED", raising=False)
    monkeypatch.delenv("MIRROR_AGENT_INTERNAL_TOKEN", raising=False)
    monkeypatch.delenv("MIRROR_AI_READ_DSN", raising=False)
    monkeypatch.setattr(server, "build_legacy_server", lambda: (legacy, 10003))
    monkeypatch.setattr(server, "build_agent_server", lambda: pytest.fail("Agent must stay disabled"))
    monkeypatch.setattr(server.time, "sleep", lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt()))

    server.serve()

    assert legacy.started is True
    assert legacy.stopped is True


def test_enabling_agent_without_token_or_read_dsn_fails_fast(monkeypatch):
    import server

    class FakeServer:
        started = False

        def start(self):
            self.started = True

    legacy = FakeServer()
    monkeypatch.setitem(server.CONFIG["agent"], "enabled", False)
    monkeypatch.setenv("MIRROR_AI_AGENT_ENABLED", "true")
    monkeypatch.setenv("MIRROR_AGENT_INTERNAL_TOKEN", "test-token")
    monkeypatch.delenv("MIRROR_AI_READ_DSN", raising=False)
    monkeypatch.setattr(server, "build_legacy_server", lambda: (legacy, 10003))

    with pytest.raises(RuntimeError, match="MIRROR_AGENT_INTERNAL_TOKEN or MIRROR_AI_READ_DSN is missing"):
        server.serve()

    assert legacy.started is False


def test_small_p95_sample_is_labeled_observation_only_and_semantics_are_grouped():
    all_cases = read_jsonl("eval/agent_v2/cases.jsonl")
    cases = [all_cases[0], next(case for case in all_cases if "citation" in case["semantic_tags"])]
    runs = []
    for case in cases:
        for engine in ("legacy", "graph_v2"):
            runs.append({
                **simulated_result(case, engine),
                "fixture_id": case["fixture_id"],
                "reference_date": case["reference_date"],
                "model_config_ref": case["model_config_ref"],
                "prompt_profile": case["prompt_profile"],
                "total_latency_ms": 120 if engine == "legacy" else 130,
                "simulated": False,
            })

    report = build_report(cases, runs)

    assert report["engines"]["legacy"]["p95_claim"] == "observation_only_insufficient_samples"
    assert report["engines"]["graph_v2"]["p95_claim"] == "observation_only_insufficient_samples"
    assert report["semantic_change_groups"]["date"]["legacy"]["runs"] == 1
    assert report["semantic_change_groups"]["citation"]["graph_v2"]["runs"] == 1
