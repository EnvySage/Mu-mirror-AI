"""Build a paired, evidence-labeled report for graph_v2 offline evaluations."""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


ENGINES = ("legacy", "graph_v2")
MIN_P95_SAMPLES = 30
PAIRING_FIELDS = ("fixture_id", "reference_date", "model_config_ref", "prompt_profile")


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with Path(path).open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"expected an object at {path}:{line_number}")
            rows.append(value)
    return rows


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def _engine_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    latencies = [float(row["total_latency_ms"]) for row in rows
                 if isinstance(row.get("total_latency_ms"), (int, float))]
    scores = [float(row["manual_score"]) for row in rows
              if isinstance(row.get("manual_score"), (int, float))]
    citation_values = [bool(row["citation_correctness"]) for row in rows
                       if isinstance(row.get("citation_correctness"), bool)]
    known_tokens = [row["tokens"] for row in rows
                    if isinstance(row.get("tokens"), dict) and row["tokens"].get("known") is True]
    return {
        "runs": len(rows),
        "outcomes": dict(sorted(Counter(row.get("outcome", "unknown") for row in rows).items())),
        "observed_p95_total_latency_ms": _p95(latencies),
        "latency_sample_count": len(latencies),
        "p95_claim": "measured_sample" if len(latencies) >= MIN_P95_SAMPLES else "observation_only_insufficient_samples",
        "manual_score_count": len(scores),
        "mean_manual_score": round(sum(scores) / len(scores), 4) if scores else None,
        "citation_checked_count": len(citation_values),
        "citation_correct_count": sum(citation_values),
        "citation_accuracy": round(sum(citation_values) / len(citation_values), 4)
        if citation_values else None,
        "token_known_run_count": len(known_tokens),
        "input_tokens_total": sum(int(row["input"]) for row in known_tokens if isinstance(row.get("input"), int)),
        "output_tokens_total": sum(int(row["output"]) for row in known_tokens if isinstance(row.get("output"), int)),
        "token_unknown_run_count": len(rows) - len(known_tokens),
    }


def build_report(cases: list[dict[str, Any]], runs: list[dict[str, Any]]) -> dict[str, Any]:
    case_by_id = {case["case_id"]: case for case in cases}
    if len(case_by_id) != len(cases):
        raise ValueError("duplicate evaluation case_id")
    for case in cases:
        missing = set(PAIRING_FIELDS) - case.keys()
        if missing:
            raise ValueError(f"case {case['case_id']} missing pairing metadata: {sorted(missing)}")

    seen: set[tuple[str, str]] = set()
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in runs:
        case_id, engine = row.get("case_id"), row.get("engine")
        if case_id not in case_by_id or engine not in ENGINES:
            raise ValueError(f"run references unknown case or engine: {case_id!r}/{engine!r}")
        case = case_by_id[case_id]
        for field in PAIRING_FIELDS:
            if field not in row or row[field] != case[field]:
                raise ValueError(f"run pairing metadata mismatch for {case_id}/{engine}: {field}")
        key = (case_id, engine)
        if key in seen:
            raise ValueError(f"duplicate result for {case_id}/{engine}")
        seen.add(key)
        grouped[engine].append(row)

    pairs: list[dict[str, Any]] = []
    for case in cases:
        pair = {engine: next((row for row in runs
                              if row.get("case_id") == case["case_id"] and row.get("engine") == engine), None)
                for engine in ENGINES}
        complete = all(
            pair[engine] is not None and pair[engine].get("executed") is not False
            for engine in ENGINES
        )
        pairs.append({
            "case_id": case["case_id"],
            "fixture_id": case["fixture_id"],
            "reference_date": case["reference_date"],
            "model_config_ref": case["model_config_ref"],
            "prompt_profile": case["prompt_profile"],
            "prompt_hashes": {engine: pair[engine].get("prompt_hash") if pair[engine] else None
                              for engine in ENGINES},
            "paired": complete,
            "simulated": bool(complete and pair["legacy"].get("simulated") and pair["graph_v2"].get("simulated")),
            "manual_score_delta_graph_minus_legacy": (
                round(float(pair["graph_v2"]["manual_score"]) - float(pair["legacy"]["manual_score"]), 4)
                if complete and isinstance(pair["graph_v2"].get("manual_score"), (int, float))
                and isinstance(pair["legacy"].get("manual_score"), (int, float)) else None
            ),
        })

    semantic_groups: dict[str, dict[str, Any]] = {}
    for tag in sorted({tag for case in cases for tag in case.get("semantic_tags", [])}):
        tagged = [case for case in cases if tag in case.get("semantic_tags", [])]
        semantic_groups[tag] = {
            "cases": [case["case_id"] for case in tagged],
            "legacy": _engine_summary([row for row in grouped["legacy"]
                                       if row["case_id"] in {case["case_id"] for case in tagged}]),
            "graph_v2": _engine_summary([row for row in grouped["graph_v2"]
                                         if row["case_id"] in {case["case_id"] for case in tagged}]),
            "observed_changes": {
                engine: [
                    {"case_id": row["case_id"], "changes": row.get("observed_changes", [])}
                    for row in grouped[engine]
                    if row["case_id"] in {case["case_id"] for case in tagged}
                    and row.get("observed_changes")
                ]
                for engine in ENGINES
            },
        }

    simulated = bool(runs) and all(bool(row.get("simulated")) for row in runs)
    live_model_runs = sum(row.get("live_model") is True for row in runs)
    live_model_calls = sum(
        int(row.get("model_call_count", 0))
        for row in runs
        if isinstance(row.get("model_call_count"), int) and not isinstance(row.get("model_call_count"), bool)
    )
    complete_pair_count = sum(bool(pair["paired"]) for pair in pairs)
    fully_live_paired = bool(runs) and live_model_runs == len(runs) and complete_pair_count == len(cases)
    execution_label = (
        "SIMULATED_HARNESS_ONLY" if simulated else
        "REAL_MODEL_PAIRED_OFFLINE" if fully_live_paired else
        "MIXED_MODEL_ADAPTER_REPLAY" if live_model_runs else
        "ADAPTER_REPLAY"
    )
    call_budgets = {
        int(row["model_call_budget"])
        for row in runs
        if isinstance(row.get("model_call_budget"), int) and not isinstance(row.get("model_call_budget"), bool)
    }
    fault_cases_not_injected = sum(row.get("fault_injection") == "not_injected" for row in runs)
    write_case_rows = [row for row in runs if row.get("write_case_note")]
    return {
        "schema_version": 1,
        "execution_label": execution_label,
        "simulated": simulated,
        "case_count": len(cases),
        "run_count": len(runs),
        "complete_pair_count": complete_pair_count,
        "not_executed_run_count": sum(row.get("executed") is False for row in runs),
        "live_model_run_count": live_model_runs,
        "live_model_call_count": live_model_calls,
        "model_call_budget": min(call_budgets) if call_budgets else None,
        "fault_injection_not_run_count": fault_cases_not_injected,
        "write_case_run_count": len(write_case_rows),
        "write_case_command_policy": "disabled; no business command endpoint configured",
        "business_write_success_count": sum(bool(row.get("business_write_success")) for row in runs),
        "pairing_basis": list(PAIRING_FIELDS),
        "quality_claim": "none_simulated" if simulated else "manual_scoring_required",
        "engines": {engine: _engine_summary(grouped[engine]) for engine in ENGINES},
        "semantic_change_groups": semantic_groups,
        "pairs": pairs,
        "runs": runs,
    }


def write_report(report: dict[str, Any], path: str | Path) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
