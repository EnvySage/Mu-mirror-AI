"""Runtime budgets, cancellation, canonical tool calls, and event sequencing."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, replace
from functools import lru_cache
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agent_core.types import AgentEvent, AgentState, RuntimeContext, ToolCall
from agent_data.scope import ReadScope


TOTAL_BUDGET_MS = 480_000
LOOP_BUDGET_MS = 180_000
ANSWER_BUDGET_MS = 180_000
MAX_PLAN_ROUNDS = 4
MAX_CALLS_PER_ROUND = 2
RECURSION_LIMIT = 64


class AgentAbort(RuntimeError):
    def __init__(self, stop_reason: str, code: str, message: str):
        super().__init__(message)
        self.stop_reason = stop_reason
        self.code = code


@dataclass(frozen=True, slots=True)
class AgentDependencies:
    """Server-owned factories and repositories; request secrets stay per invocation."""

    repositories: Any
    llm_factory: Any = None
    embedding_factory: Any = None
    clock: Any = time.monotonic
    max_total_budget_ms: int = TOTAL_BUDGET_MS
    max_loop_budget_ms: int = LOOP_BUDGET_MS
    answer_budget_ms: int = ANSWER_BUDGET_MS
    command_client: Any = None


def canonical_tool_args(args: dict[str, Any]) -> str:
    """Stable recursive JSON form used for duplicate-call detection."""
    if not isinstance(args, dict):
        raise ValueError("tool args must be an object")
    return json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def call_fingerprint(call: ToolCall) -> str:
    return f"{call.tool}:{canonical_tool_args(call.args)}"


def make_runtime(request: Any, dependencies: AgentDependencies, cancellation: Any = None) -> RuntimeContext:
    """Validate trusted run inputs and build private per-run clients and scopes."""
    from datetime import date
    from embedding.factory import create_embedder
    from llm.factory import create_llm

    run_id = _required_text(request, "run_id", maximum=128)
    user_id = _parse_uuid(_required_text(request, "user_id", maximum=64), "user_id")
    session_id = _parse_uuid(_required_text(request, "session_id", maximum=64), "session_id")
    question = _required_text(request, "question", maximum=20_000)
    user_message_id = getattr(request, "user_message_id", 0)
    if isinstance(user_message_id, bool) or not isinstance(user_message_id, int) or user_message_id <= 0:
        raise ValueError("user_message_id must be positive")
    reference_date_raw = _required_text(request, "reference_date", maximum=10)
    try:
        reference_date = date.fromisoformat(reference_date_raw)
    except ValueError as exc:
        raise ValueError("reference_date must use YYYY-MM-DD") from exc
    if reference_date.isoformat() != reference_date_raw:
        raise ValueError("reference_date must use YYYY-MM-DD")
    timezone = _required_text(request, "timezone", maximum=64)
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("timezone is unknown") from exc

    allowed_write_tools = tuple(getattr(request, "allowed_write_tools", ()) or ())
    command_grant = getattr(request, "command_grant", "")
    authorized_record_content = getattr(request, "authorized_record_content", "")
    has_write_input = bool(allowed_write_tools or command_grant or authorized_record_content)
    if getattr(request, "shadow", False) and has_write_input:
        raise ValueError("shadow runs cannot carry a write grant")
    if has_write_input:
        if (allowed_write_tools != ("create_record",) or not isinstance(command_grant, str)
                or not command_grant.strip() or not isinstance(authorized_record_content, str)
                or not authorized_record_content.strip() or _utf16_units(authorized_record_content) > 2000):
            raise ValueError("create_record grant input is incomplete or invalid")
        if dependencies.command_client is None:
            raise ValueError("create_record command client is unavailable")
    elif allowed_write_tools or command_grant or authorized_record_content:
        raise ValueError("write grant input is inconsistent")

    total_ms = _bounded_budget(
        getattr(request, "total_budget_ms", 0), dependencies.max_total_budget_ms, TOTAL_BUDGET_MS,
    )
    loop_ms = _bounded_budget(
        getattr(request, "loop_budget_ms", 0), dependencies.max_loop_budget_ms, LOOP_BUDGET_MS,
    )
    requested_rounds = getattr(request, "max_plan_rounds", 0)
    if isinstance(requested_rounds, bool) or not isinstance(requested_rounds, int) or requested_rounds < 0:
        raise ValueError("max_plan_rounds must be non-negative")
    max_rounds = min(MAX_PLAN_ROUNDS, requested_rounds) if requested_rounds else MAX_PLAN_ROUNDS
    half_life_days = getattr(request, "half_life_days", 30.0) or 30.0
    if isinstance(half_life_days, bool) or not isinstance(half_life_days, (int, float)) or half_life_days <= 0:
        raise ValueError("half_life_days must be positive")

    start = dependencies.clock()
    total_deadline = start + total_ms / 1000.0
    loop_deadline = min(total_deadline, start + loop_ms / 1000.0)
    answer_budget_ms = min(dependencies.answer_budget_ms, ANSWER_BUDGET_MS)
    llm_config = getattr(request, "llm_config", None)
    embedding_config = getattr(request, "embedding_config", None)
    if llm_config is None or not getattr(llm_config, "api_key", "") or not getattr(llm_config, "model", ""):
        raise ValueError("llm_config is required")
    remaining = max(0.001, total_deadline - dependencies.clock())
    llm_factory = dependencies.llm_factory or create_llm
    embedding_factory = dependencies.embedding_factory or create_embedder
    llm = llm_factory(
        provider=llm_config.provider,
        api_key=llm_config.api_key,
        base_url=llm_config.base_url,
        model=llm_config.model,
        protocol=llm_config.protocol,
        timeout=remaining,
        max_retries=0,
    )
    embedding_client = None
    # Explicit Java-authorized writes have a deterministic path and never enter
    # retrieval. Avoid loading an embedding model for a request that cannot use it.
    if (not has_write_input and embedding_config is not None
            and getattr(embedding_config, "source", "")):
        embedding_client = embedding_factory(
            source=embedding_config.source,
            local_model=embedding_config.local_model,
            api_provider=embedding_config.api_provider,
            api_key=embedding_config.api_key,
            api_model=embedding_config.api_model,
            base_url=embedding_config.base_url,
            timeout=min(30.0, remaining),
            max_retries=0,
        )

    read_scope = ReadScope(user_id=user_id, session_id=session_id, user_message_id=user_message_id)
    return RuntimeContext(
        user_id=user_id,
        session_id=session_id,
        run_id=run_id,
        user_message_id=user_message_id,
        reference_date=reference_date,
        timezone=timezone,
        read_scope=read_scope,
        llm=llm,
        llm_config=llm_config,
        embedding_client=embedding_client,
        command_client=dependencies.command_client if has_write_input else None,
        command_grant=command_grant if has_write_input else None,
        authorized_record_content=authorized_record_content if has_write_input else None,
        allowed_write_tools=allowed_write_tools if has_write_input else (),
        deadline=total_deadline,
        total_deadline=total_deadline,
        cancelled=cancellation,
        repositories=dependencies.repositories,
        loop_deadline=loop_deadline,
        max_plan_rounds=max_rounds,
        half_life_days=float(half_life_days),
        shadow=bool(getattr(request, "shadow", False)),
        answer_budget_ms=answer_budget_ms,
        clock=dependencies.clock,
    )


def _utf16_units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def run_agent(request: Any, dependencies: AgentDependencies, cancellation: Any = None):
    """Run one fully isolated graph invocation and yield only typed custom events."""
    runtime = None
    terminal_emitted = False
    try:
        runtime = make_runtime(request, dependencies, cancellation)
        graph = _compiled_graph()
        initial = AgentState(question=request.question)
        stream = graph.stream(
            initial,
            context=runtime,
            stream_mode="custom",
            config={"recursion_limit": RECURSION_LIMIT},
        )
        try:
            for payload in stream:
                check_control(runtime)
                if not isinstance(payload, dict) or payload.get("kind") not in {
                    "meta", "thinking", "delta", "tool_finished", "completed", "failed",
                }:
                    continue
                kind = payload["kind"]
                if kind in {"completed", "failed"}:
                    if terminal_emitted:
                        continue
                    terminal_emitted = True
                yield AgentEvent(
                    kind=kind,
                    content=payload.get("content", "") if isinstance(payload.get("content", ""), str) else "",
                    metadata=payload.get("metadata", {}) if isinstance(payload.get("metadata", {}), dict) else {},
                )
        finally:
            _close(stream)
        if not terminal_emitted:
            yield AgentEvent(
                kind="failed",
                metadata={
                    "code": "MISSING_TERMINAL",
                    "message": "agent ended without a terminal event",
                    "has_partial_answer": False,
                    "stop_reason": "planner_error",
                },
            )
    except AgentAbort as exc:
        if not terminal_emitted:
            yield _failed_event(exc.code, str(exc), exc.stop_reason)
    except (TypeError, ValueError) as exc:
        if not terminal_emitted:
            yield _failed_event("INVALID_REQUEST", str(exc)[:256], "planner_error")
    except Exception:
        if not terminal_emitted:
            yield _failed_event("AGENT_ERROR", "本轮处理失败", "read_error")
    finally:
        if runtime is not None:
            close_runtime(runtime)


@lru_cache(maxsize=1)
def _compiled_graph():
    from agent_core.graph import build_graph

    return build_graph()


def close_runtime(runtime: RuntimeContext) -> None:
    """Close per-request SDK clients; repository pools belong to the server lifecycle."""
    seen: set[int] = set()
    for resource in (runtime.llm, runtime.embedding_client):
        for candidate in (resource, getattr(resource, "client", None)):
            if candidate is None or id(candidate) in seen:
                continue
            seen.add(id(candidate))
            close = getattr(candidate, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass


def check_control(runtime: RuntimeContext, *, deadline: float | None = None) -> None:
    cancelled = runtime.cancelled
    if cancelled is not None:
        try:
            is_cancelled = cancelled() if callable(cancelled) else bool(cancelled.is_set())
        except Exception:
            is_cancelled = True
        if is_cancelled:
            raise AgentAbort("cancelled", "CANCELLED", "agent request was cancelled")
    total_deadline = runtime.total_deadline if runtime.total_deadline is not None else runtime.deadline
    effective = min(value for value in (total_deadline, deadline) if value is not None) if (
        total_deadline is not None or deadline is not None
    ) else None
    now = runtime.clock() if runtime.clock is not None else time.monotonic()
    if effective is not None and now >= effective:
        loop_deadline = runtime.loop_deadline
        if total_deadline is not None and now >= total_deadline:
            reason = "total_deadline"
        elif loop_deadline is not None and effective == loop_deadline and (
            total_deadline is None or loop_deadline < total_deadline
        ):
            reason = "loop_budget"
        else:
            reason = "total_deadline"
        raise AgentAbort(reason, "DEADLINE_EXCEEDED", "agent budget expired")


def runtime_with_deadline(runtime: RuntimeContext, deadline: float | None) -> RuntimeContext:
    """Create a per-phase context view while preserving the hard total deadline."""
    return replace(runtime, deadline=deadline)


def _required_text(request: Any, name: str, *, maximum: int) -> str:
    value = getattr(request, name, "")
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be a non-empty string of at most {maximum} characters")
    return value.strip()


def _parse_uuid(value: str, name: str) -> UUID:
    try:
        return UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{name} must be a UUID") from exc


def _bounded_budget(value: Any, maximum: int, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("budget values must be non-negative integers")
    return min(value or default, maximum, default)


def _failed_event(code: str, message: str, stop_reason: str) -> AgentEvent:
    return AgentEvent(
        kind="failed",
        metadata={
            "code": code,
            "message": message,
            "has_partial_answer": False,
            "stop_reason": stop_reason,
        },
    )


def _close(stream: Any) -> None:
    close = getattr(stream, "close", None)
    if callable(close):
        close()

