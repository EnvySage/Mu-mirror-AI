"""Real, local Java HTTP/SSE adapter for the Task 10 paired replay.

The adapter requires an externally stored test-user map and a read-only evaluation
database DSN. It only accepts loopback Java and metering-proxy URLs. The Python
runtime is launched without MIRROR_AGENT_COMMAND_BASE_URL, so no diary write can
reach the Java business command endpoint during this offline run.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg


DEFAULT_LEGACY_URL = "http://127.0.0.1:19052/api"
DEFAULT_GRAPH_URL = "http://127.0.0.1:19051/api"
DEFAULT_METER_URL = "http://127.0.0.1:19070/_meter"
DEFAULT_MODEL = "cn:deepseek-v4.1-flash"

_USERS: dict[str, Any] | None = None
_SESSIONS: dict[str, Any] | None = None
_DATABASE_DSN: str | None = None


def _load_json_env(name: str, cache_name: str) -> dict[str, Any]:
    path = os.getenv(name, "").strip()
    if not path:
        raise RuntimeError(f"{name} must point to an external Task 10 fixture file")
    loaded = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(loaded, dict):
        raise RuntimeError(f"{name} must contain a JSON object")
    return loaded


def _users() -> dict[str, Any]:
    global _USERS
    if _USERS is None:
        _USERS = _load_json_env("MIRROR_TASK10_USERS_FILE", "users")
    return _USERS


def _sessions() -> dict[str, Any]:
    global _SESSIONS
    if _SESSIONS is None:
        loaded = _load_json_env("MIRROR_TASK10_SESSIONS_FILE", "sessions")
        sessions = loaded.get("sessions")
        if not isinstance(sessions, dict):
            raise RuntimeError("external session file is missing its sessions object")
        _SESSIONS = sessions
    return _SESSIONS


def _database_dsn() -> str:
    global _DATABASE_DSN
    if _DATABASE_DSN is None:
        _DATABASE_DSN = os.getenv("MIRROR_TASK10_APP_DSN", "").strip()
        if not _DATABASE_DSN:
            raise RuntimeError("MIRROR_TASK10_APP_DSN is required to read run metrics")
    return _DATABASE_DSN


def _require_loopback(url: str) -> None:
    hostname = urllib.parse.urlsplit(url).hostname
    if hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("offline adapter only accepts loopback service URLs")


def _service_url(engine: str) -> str:
    variable = "MIRROR_TASK10_GRAPH_URL" if engine == "graph_v2" else "MIRROR_TASK10_LEGACY_URL"
    default = DEFAULT_GRAPH_URL if engine == "graph_v2" else DEFAULT_LEGACY_URL
    value = os.getenv(variable, default).strip().rstrip("/")
    _require_loopback(value)
    return value + "/mirror/chat"


def _meter_count() -> int | None:
    url = os.getenv("MIRROR_TASK10_METER_URL", DEFAULT_METER_URL).strip()
    _require_loopback(url)
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            payload = json.load(response)
        value = payload.get("calls")
        return value if isinstance(value, int) and not isinstance(value, bool) else None
    except (OSError, ValueError, urllib.error.URLError):
        return None


def _meter_state() -> dict[str, Any]:
    url = os.getenv("MIRROR_TASK10_METER_URL", DEFAULT_METER_URL).strip()
    _require_loopback(url)
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            payload = json.load(response)
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError, urllib.error.URLError):
        return {}


def _budget_exhausted_row(case: dict[str, Any], engine: str, call_budget: int | None) -> dict[str, Any]:
    return {
        "case_id": case["case_id"],
        "engine": engine,
        "fixture_id": case["fixture_id"],
        "reference_date": case["reference_date"],
        "model_config_ref": case["model_config_ref"],
        "prompt_profile": case["prompt_profile"],
        "prompt_hash": None,
        "outcome": "not_executed",
        "stop_reason": "MODEL_CALL_BUDGET_EXHAUSTED",
        "phase_latency_ms": {},
        "total_latency_ms": None,
        "first_thinking_ms": None,
        "first_body_ms": None,
        "first_body_text": "",
        "tool_call_count": 0,
        "tools_used": [],
        "sources": [],
        "citation_correctness": None,
        "manual_score": None,
        "tokens": {"input": None, "output": None, "known": False},
        "model": DEFAULT_MODEL,
        "model_call_count": 0,
        "model_call_budget": call_budget,
        "live_model": False,
        "executed": False,
        "http_status": None,
        "run_id": None,
        "observed_changes": [],
        "business_write_success": False,
        "write_policy": "command_client_disabled",
        "write_case_note": "no business write endpoint was configured" if case.get("category") == "writes" else None,
        "fault_injection": "not_injected" if case.get("category") == "failures" else None,
        "simulated": False,
    }


def _read_sse(response) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    event_name = "message"
    data_lines: list[str] = []

    def flush() -> None:
        nonlocal event_name, data_lines
        if not data_lines:
            event_name = "message"
            return
        payload = "\n".join(data_lines)
        data_lines = []
        current_name = event_name
        event_name = "message"
        if payload.strip() == "[DONE]":
            return
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return
        if isinstance(data, dict):
            events.append((current_name, data))
        elif isinstance(data, list):
            events.append((current_name, {"items": data}))

    for raw_line in response:
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            flush()
        elif line.startswith(":"):
            continue
        elif line.startswith("event:"):
            event_name = line[6:].strip() or "message"
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    flush()
    return events


def _database_observation(run_id: str | None, user_id: str) -> dict[str, Any]:
    if not run_id:
        return {}
    try:
        with psycopg.connect(_database_dsn(), connect_timeout=5) as connection:
            row = connection.execute(
                "SELECT outcome, stop_reason, tool_call_count, metrics, assistant_message_id "
                "FROM public.agent_runs WHERE run_id = %s::uuid AND user_id = %s::uuid",
                (run_id, user_id),
            ).fetchone()
            if row is None:
                return {}
            outcome, stop_reason, tool_count, metrics, assistant_id = row
            assistant = None
            if assistant_id is not None:
                assistant = connection.execute(
                    "SELECT content, sources, tools_used FROM public.conversation_history WHERE id = %s",
                    (assistant_id,),
                ).fetchone()
            if isinstance(metrics, str):
                try:
                    metrics = json.loads(metrics)
                except json.JSONDecodeError:
                    metrics = {}
            if not isinstance(metrics, dict):
                metrics = {}
            result: dict[str, Any] = {
                "outcome": outcome,
                "stop_reason": stop_reason,
                "tool_call_count": int(tool_count or 0),
                "metrics": metrics,
            }
            if assistant:
                result["assistant_text"] = assistant[0]
                result["sources"] = assistant[1] or []
                result["tools_used"] = assistant[2] or []
            return result
    except (psycopg.Error, OSError, ValueError):
        return {}


def run_case(case: dict[str, Any], engine: str) -> dict[str, Any]:
    """Run one synthetic case through the requested local Java engine."""
    if engine not in {"legacy", "graph_v2"}:
        raise ValueError("engine must be legacy or graph_v2")
    fixture_id = case["fixture_id"]
    user = _users().get(fixture_id)
    if not isinstance(user, dict) or not user.get("token") or not user.get("user_id"):
        raise RuntimeError(f"external user file has no credentials for {fixture_id}")
    case_sessions = _sessions().get(case["case_id"], {})
    session_id = case_sessions.get(engine) if isinstance(case_sessions, dict) else None
    if not session_id:
        raise RuntimeError(f"external session file has no {engine} session for {case['case_id']}")

    url = _service_url(engine)
    meter_before = _meter_state()
    before_calls = meter_before.get("calls") if isinstance(meter_before.get("calls"), int) else None
    meter_limit = meter_before.get("limit") if isinstance(meter_before.get("limit"), int) else None
    try:
        configured_budget = int(os.getenv("MIRROR_TASK10_CALL_BUDGET", "0")) or None
    except ValueError:
        configured_budget = None
    if before_calls is None or meter_limit is None:
        raise RuntimeError("local model-call meter is unavailable; refusing an unmetered evaluation request")
    if meter_before.get("budget_exhausted") is True or (
        meter_limit is not None and before_calls is not None and before_calls >= meter_limit
    ):
        return _budget_exhausted_row(case, engine, configured_budget or meter_limit)
    request_id = str(uuid4())
    body = json.dumps({
        "question": case["question"],
        "sessionId": session_id,
        "requestId": request_id,
    }, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": "Bearer " + user["token"],
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        method="POST",
    )

    started = time.perf_counter()
    status = 200
    events: list[tuple[str, dict[str, Any]]] = []
    transport_error: str | None = None
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            status = int(response.status)
            events = _read_sse(response)
    except urllib.error.HTTPError as error:
        status = int(error.code)
        transport_error = f"HTTP_{status}"
    except (OSError, TimeoutError, urllib.error.URLError):
        transport_error = "TRANSPORT_ERROR"
    total_latency_ms = round((time.perf_counter() - started) * 1000, 3)
    after_calls = _meter_count()
    model_call_count = max(0, after_calls - before_calls) if before_calls is not None and after_calls is not None else 0

    answer_parts: list[str] = []
    thinking_seen = False
    first_thinking_ms: float | None = None
    first_body_ms: float | None = None
    run_id: str | None = None
    done: dict[str, Any] = {}
    error_event: dict[str, Any] = {}
    event_tools: list[str] = []
    event_sources: list[dict[str, Any]] = []
    for name, data in events:
        elapsed = round((time.perf_counter() - started) * 1000, 3)
        if name == "meta":
            run_id = data.get("runId") or run_id
            event_tools.extend(str(value) for value in data.get("tools_used", []) or [])
        elif name == "thinking":
            content = data.get("content")
            if isinstance(content, str) and content:
                thinking_seen = True
                if first_thinking_ms is None:
                    first_thinking_ms = elapsed
        elif name == "delta":
            content = data.get("content")
            if isinstance(content, str) and content:
                answer_parts.append(content)
                if first_body_ms is None:
                    first_body_ms = elapsed
        elif name == "sources":
            if isinstance(data.get("items"), list):
                event_sources.extend(item for item in data["items"] if isinstance(item, dict))
            else:
                event_sources.append(data)
        elif name in {"tool_finished", "tool"}:
            event_tools.append(str(data.get("tool", name)))
        elif name == "done":
            done = data
            run_id = data.get("runId") or run_id
        elif name == "error":
            error_event = data

    db = _database_observation(run_id, user["user_id"])
    answer_text = "".join(answer_parts)
    if not answer_text and isinstance(db.get("assistant_text"), str):
        answer_text = db["assistant_text"]
    source_rows = db.get("sources") if db.get("sources") else event_sources
    observed_tool_names = db.get("tools_used") if db.get("tools_used") else event_tools
    metrics = db.get("metrics") if isinstance(db.get("metrics"), dict) else {}
    phase_latency = metrics.get("phase_latency_ms", {})
    if not isinstance(phase_latency, dict):
        phase_latency = {}
    prompt_hashes = metrics.get("prompt_hashes")
    prompt_hash = json.dumps(prompt_hashes, sort_keys=True) if prompt_hashes else None
    outcome = db.get("outcome") or done.get("outcome")
    if not outcome:
        outcome = "error" if error_event or transport_error else ("answer" if answer_text else "empty")
    stop_reason = db.get("stop_reason") or done.get("stop_reason") or error_event.get("code") or transport_error
    if not stop_reason and status != 200:
        stop_reason = f"HTTP_{status}"
    if first_body_ms is None and answer_text:
        first_body_ms = total_latency_ms
    row: dict[str, Any] = {
        "case_id": case["case_id"],
        "engine": engine,
        "fixture_id": fixture_id,
        "reference_date": case["reference_date"],
        "model_config_ref": case["model_config_ref"],
        "prompt_profile": case["prompt_profile"],
        "prompt_hash": prompt_hash,
        "outcome": outcome,
        "stop_reason": stop_reason,
        "phase_latency_ms": {str(key): value for key, value in phase_latency.items()},
        "total_latency_ms": total_latency_ms,
        "first_thinking_ms": first_thinking_ms if thinking_seen else None,
        "first_body_ms": first_body_ms,
        "first_body_text": answer_text,
        "tool_call_count": db.get("tool_call_count", len(observed_tool_names)),
        "tools_used": observed_tool_names,
        "sources": source_rows,
        "citation_correctness": None,
        "manual_score": None,
        "tokens": {"input": None, "output": None, "known": False},
        "model": DEFAULT_MODEL,
        "model_call_count": model_call_count,
        "model_call_budget": configured_budget,
        "live_model": model_call_count > 0,
        "executed": True,
        "http_status": status,
        "run_id": run_id,
        "observed_changes": [],
        "business_write_success": False,
        "write_policy": "command_client_disabled",
        "write_case_note": "no business write endpoint was configured" if case.get("category") == "writes" else None,
        "fault_injection": "not_injected" if case.get("category") == "failures" else None,
        "simulated": False,
    }
    return row


__all__ = ["run_case"]
