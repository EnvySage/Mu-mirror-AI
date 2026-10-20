"""Run paired legacy/graph_v2 fixtures through an explicit offline adapter."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path
from typing import Any, Callable

try:  # package import in tests
    from .report import build_report, read_jsonl, write_report
except ImportError:  # direct script execution
    from report import build_report, read_jsonl, write_report


DEFAULT_CASES = Path(__file__).with_name("cases.jsonl")
DEFAULT_OUTPUT = Path(__file__).with_name("reports") / "latest.json"


def load_adapter(reference: str) -> Callable[[dict[str, Any], str], dict[str, Any]]:
    module_name, separator, function_name = reference.partition(":")
    if not separator or not module_name or not function_name:
        raise ValueError("adapter must use module.path:function_name")
    module = importlib.import_module(module_name)
    adapter = getattr(module, function_name)
    if not callable(adapter):
        raise ValueError("adapter target is not callable")
    return adapter


def simulated_result(case: dict[str, Any], engine: str) -> dict[str, Any]:
    """Exercise pairing/report code without claiming either engine was run."""
    return {
        "case_id": case["case_id"],
        "engine": engine,
        "fixture_id": case["fixture_id"],
        "reference_date": case["reference_date"],
        "model_config_ref": case["model_config_ref"],
        "prompt_profile": case["prompt_profile"],
        "prompt_hash": None,
        "outcome": "not_executed",
        "stop_reason": "SIMULATED",
        "phase_latency_ms": {},
        "total_latency_ms": None,
        "first_thinking_ms": None,
        "first_body_ms": None,
        "first_body_text": "",
        "tool_call_count": None,
        "citation_correctness": None,
        "manual_score": None,
        "tokens": {"input": None, "output": None, "known": False},
        "observed_changes": [],
        "business_write_success": False,
        "simulated": True,
    }


def replay(cases: list[dict[str, Any]], adapter: Callable[[dict[str, Any], str], dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for case in cases:
        required = {
            "case_id", "fixture_id", "question", "history", "reference_date",
            "model_config_ref", "prompt_profile",
        }
        missing = required - case.keys()
        if missing:
            raise ValueError(f"case {case.get('case_id', '<unknown>')} missing {sorted(missing)}")
        for engine in ("legacy", "graph_v2"):
            result = adapter(case, engine)
            if not isinstance(result, dict):
                raise ValueError(f"adapter must return a dict for {case['case_id']}/{engine}")
            expected_identity = {
                "case_id": case["case_id"],
                "engine": engine,
                "fixture_id": case["fixture_id"],
                "reference_date": case["reference_date"],
                "model_config_ref": case["model_config_ref"],
                "prompt_profile": case["prompt_profile"],
            }
            for field, expected in expected_identity.items():
                if field not in result or result[field] != expected:
                    raise ValueError(
                        f"adapter pairing metadata mismatch for {case['case_id']}/{engine}: {field}"
                    )
            rows.append(dict(result))
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--adapter", help="offline adapter as module.path:function")
    parser.add_argument("--simulated", action="store_true", help="write a harness-only report; no engine is called")
    args = parser.parse_args(argv)
    if bool(args.adapter) == bool(args.simulated):
        parser.error("choose exactly one of --adapter or --simulated")

    cases = read_jsonl(args.cases)
    adapter = (lambda case, engine: simulated_result(case, engine)) if args.simulated else load_adapter(args.adapter)
    rows = replay(cases, adapter)
    report = build_report(cases, rows)
    write_report(report, args.output)
    print(json.dumps({
        "execution_label": report["execution_label"],
        "case_count": report["case_count"],
        "run_count": report["run_count"],
        "complete_pair_count": report["complete_pair_count"],
        "output": str(args.output),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
