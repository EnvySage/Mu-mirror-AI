"""Sequential, read-only nodes for the stateless graph_v2 conversation flow."""

from __future__ import annotations

import json
import hashlib
import time
from dataclasses import replace
from datetime import datetime, time as day_time
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from langgraph.config import get_stream_writer
from langgraph.runtime import Runtime

from agent_core.evidence import assign_citations, merge_evidence
from agent_core.material import answer_evidence, assess_material, has_material
from agent_core.retrieval import retrieve
from agent_core.runner import AgentAbort, MAX_CALLS_PER_ROUND, check_control
from agent_core.types import AgentState, CreatedRecord, RuntimeContext, ToolCall, ToolObservation
from agent_tools.read_tools import execute_read_tool
from agent_tools.registry import get_read_tool_specs
from mirror_core.llm_json import parse_json
from mirror_core.errors import ContentInvalidError
from services import answer_service, intent_service


def prepare(state: AgentState, runtime: Runtime[RuntimeContext]) -> dict[str, Any]:
    context = runtime.context
    check_control(context)
    from agent_core.context_loader import load_context

    prepared = load_context(context.read_scope, state.question, context.repositories)
    return {
        "prepared_context": prepared,
        "history_plan": list(prepared.history_plan),
        "history_answer": list(prepared.history_answer),
        "glossary": list(prepared.glossary_top30),
    }


def understand(state: AgentState, runtime: Runtime[RuntimeContext]) -> dict[str, Any]:
    context = runtime.context
    check_control(context)
    prepared = state.prepared_context
    if prepared is None:
        raise RuntimeError("prepared context is missing")
    result = intent_service.understand(state.question, prepared, context)
    return {
        "intent": result,
        "standalone_question": result.standalone_question,
        "action_kind": result.action_kind,
        "route": result.route,
    }


def route_action(state: AgentState) -> str:
    if state.action_kind == "read":
        return "retrieve"
    if state.action_kind == "clarification":
        return "clarify"
    if state.action_kind == "create_record":
        return "create_record"
    if state.action_kind == "prepare_action":
        return "prepare_action"
    return "synthesize"


def prepare_action(state: AgentState, runtime: Runtime[RuntimeContext]) -> dict[str, Any]:
    """Ask Java to create one confirmation card; never execute a business write in Python."""
    context = runtime.context
    check_control(context)
    intent = state.intent
    if (intent is None or not intent.prepare_tool or not isinstance(intent.prepare_args, dict)
            or intent.prepare_tool not in {item.get("name") for item in context.prepare_tools}):
        raise RuntimeError("validated prepare action is missing")
    call_id = uuid4().hex
    emit(
        runtime,
        "prepare_action",
        call_id=call_id,
        tool=intent.prepare_tool,
        args_json=json.dumps(intent.prepare_args, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False),
    )
    return {
        "answer": "",
        "sources": [],
        "vault_refs": [],
        "outcome": "action_prepared",
        "stop_reason": "action_prepared",
        "fallback": False,
        "tool_call_count": 1,
    }


def retrieve_evidence(state: AgentState, runtime: Runtime[RuntimeContext]) -> dict[str, Any]:
    context = runtime.context
    check_control(context)
    if state.intent is None:
        raise RuntimeError("intent is missing")
    if state.intent.file_query_mode in {"body", "list"}:
        return {"retrieval_summary": "文件任务通过 find_item 查找目标，通过 recall_item 读取正文。"}
    reference_time = datetime.combine(
        context.reference_date, day_time.max, tzinfo=ZoneInfo(context.timezone),
    )
    try:
        result = retrieve(
            state.intent, context.read_scope, context.repositories, context.embedding_client,
            half_life_days=context.half_life_days, deadline=context.deadline,
            reference_time=reference_time, clock=context.clock,
        )
    except Exception:
        check_control(context)
        return {"retrieval_summary": "初始检索暂不可用，可以通过只读工具继续核对；不表示未找到资料。",
                "degraded_reasons": ["initial_retrieval:UNAVAILABLE"]}
    return {
        "route": result.route,
        "evidence": merge_evidence((), result.evidence),
        "retrieval_summary": result.summary,
        "degraded_reasons": [result.degradation_reason] if result.degradation_reason else [],
    }


def plan(state: AgentState, runtime: Runtime[RuntimeContext]) -> dict[str, Any]:
    context = runtime.context
    try:
        check_control(context, deadline=context.loop_deadline)
    except AgentAbort as exc:
        if exc.stop_reason == "loop_budget":
            return {"pending_calls": [], "pending_auto_backstop": [], "stop_reason": "loop_budget"}
        raise
    if state.plan_rounds >= context.max_plan_rounds:
        return {"pending_calls": [], "pending_auto_backstop": [], "stop_reason": "max_rounds"}
    if context.llm is None:
        return {"pending_calls": [], "pending_auto_backstop": [], "stop_reason": "planner_error", "error_code": "LLM_MISSING"}

    step = state.plan_rounds + 1
    messages = answer_service.build_agent_plan_messages(
        state.standalone_question or state.question,
        state.history_plan,
        state.glossary,
        get_read_tool_specs(),
        state.observations,
        retrieval_summary=state.retrieval_summary,
        evidence=state.evidence,
        step=step,
        max_steps=context.max_plan_rounds,
        today=context.reference_date.isoformat(),
        assistant_context=answer_service.assistant_context_text(context.assistant_context),
    )
    deadline = _phase_deadline(context, context.loop_deadline)
    timeout = _remaining(deadline, context)
    stream = None
    content: list[str] = []
    try:
        planner = getattr(context.llm, "plan", None)
        if callable(planner):
            parsed = planner(messages, get_read_tool_specs(), protocol=context.planning_protocol,
                             timeout=timeout, max_retries=0)
        else:
            stream = context.llm.chat_stream(
                messages, thinking_budget=0, timeout=timeout, max_retries=0,
            )
            for kind, text in stream:
                check_control(context, deadline=context.loop_deadline)
                if kind == "thinking" and text:
                    emit(runtime, "thinking", text)
                elif kind in {"content", "delta"} and text:
                    content.append(text)
            parsed = parse_json("".join(content), ctx="graph_v2 工具规划")
        check_control(context, deadline=context.loop_deadline)
    except AgentAbort as exc:
        if exc.stop_reason == "loop_budget":
            return {
                "plan_rounds": step,
                "pending_calls": [],
                "pending_auto_backstop": [],
                "final_batch": False,
                "plan_done": False,
                "stop_reason": "loop_budget",
                "error_code": "",
            }
        raise
    except ContentInvalidError:
        return _invalid_plan(state, step)
    except Exception:
        failure = ToolObservation(uuid4().hex, "planner", False, "UNAVAILABLE", "工具规划暂不可用，保留现有证据。")
        return {"plan_rounds": step, "pending_calls": [], "pending_auto_backstop": [],
                "final_batch": False, "plan_done": False, "stop_reason": "planner_error",
                "error_code": "PLANNER_UNAVAILABLE", "observations": [*state.observations, failure],
                "degraded_reasons": [*state.degraded_reasons, "planner:UNAVAILABLE"]}
    finally:
        _close_stream(stream)

    if not isinstance(parsed, dict):
        return _invalid_plan(state, step)
    raw_calls = parsed.get("calls")
    if (not isinstance(raw_calls, list) or len(raw_calls) > MAX_CALLS_PER_ROUND
            or not isinstance(parsed.get("done"), bool)
            or parsed.get("finish_reason") in {"length", "max_tokens"}):
        return _invalid_plan(state, step)

    calls: list[ToolCall] = []
    auto_flags: list[bool] = []
    fingerprints = list(state.fingerprints)
    for raw in raw_calls:
        if not isinstance(raw, dict) or not isinstance(raw.get("tool"), str):
            return _invalid_plan(state, step)
        tool = raw["tool"]
        args = raw.get("args", {})
        if not isinstance(args, dict):
            return _invalid_plan(state, step)
        args = dict(args)
        auto_backstop = False
        if tool == "recall_item" and not args.get("vault_item_id"):
            candidate_ids = _candidate_ids(state.observations)
            if len(candidate_ids) != 1:
                # Let argument validation report the missing target to the planner.
                candidate_ids = []
            if candidate_ids:
                args["vault_item_id"] = candidate_ids[0]
                auto_backstop = True
        call = ToolCall(call_id=uuid4().hex, tool=tool, args=args)
        from agent_core.runner import call_fingerprint

        fingerprint = call_fingerprint(call)
        previous_failure = any(item.tool == tool and not item.success for item in state.observations)
        if fingerprint in fingerprints and not previous_failure:
            return {
                "plan_rounds": step,
                "pending_calls": [],
                "pending_auto_backstop": [],
                "final_batch": False,
                "plan_done": False,
                "stop_reason": "duplicate_call",
                "error_code": "",
            }
        fingerprints.append(fingerprint)
        calls.append(call)
        auto_flags.append(auto_backstop)

    done = parsed.get("done") is True
    stop_reason = "empty_calls" if not calls else ""
    return {
        "plan_rounds": step,
        "pending_calls": calls,
        "pending_auto_backstop": auto_flags,
        "fingerprints": fingerprints,
        "plan_done": done,
        "final_batch": bool(done and calls),
        "stop_reason": stop_reason,
        "error_code": "",
        "planning_protocol": str(parsed.get("protocol", "text_json")),
        "plan_finish_reason": str(parsed.get("finish_reason") or ""),
        "prompt_hashes": {**state.prompt_hashes, "agent_plan": hashlib.sha256(messages[0]["content"].encode()).hexdigest()},
    }


def _invalid_plan(state: AgentState, step: int) -> dict[str, Any]:
    observation = ToolObservation(uuid4().hex, "planner", False, "INVALID_PLAN",
                                  "工具规划格式无效，请按当前注册工具和参数重新规划；不要将格式错误解释为未找到资料。")
    return {"plan_rounds": step, "pending_calls": [], "pending_auto_backstop": [],
            "final_batch": False, "plan_done": False, "stop_reason": "planner_retry",
            "error_code": "PLANNER_INVALID_RESPONSE", "observations": [*state.observations, observation]}


def after_plan(state: AgentState, runtime: Runtime[RuntimeContext]) -> str:
    context = runtime.context
    if state.pending_calls:
        return "act"
    if state.stop_reason == "planner_retry" and state.plan_rounds < context.max_plan_rounds:
        return "plan"
    return "backstop"


def act(state: AgentState, runtime: Runtime[RuntimeContext]) -> dict[str, Any]:
    context = runtime.context
    observations = list(state.observations)
    evidence = list(state.evidence)
    count = state.tool_call_count
    degraded = list(state.degraded_reasons)
    retry_plan = False
    read_failure = ""
    for index, call in enumerate(state.pending_calls):
        try:
            check_control(context, deadline=context.loop_deadline)
        except AgentAbort as exc:
            if exc.stop_reason == "loop_budget":
                return {
                    "observations": observations,
                    "evidence": evidence,
                    "tool_call_count": count,
                    "stop_reason": "loop_budget",
                }
            raise
        auto = index < len(state.pending_auto_backstop) and state.pending_auto_backstop[index]
        tool_runtime = _with_effective_deadline(context, context.loop_deadline)
        observation = execute_read_tool(call, tool_runtime)
        if not observation.success:
            observation = replace(observation, data={"requested_args": call.args})
            degraded.append(f"{call.tool}:{observation.code}")
        retry_plan = retry_plan or observation.code in {"UNKNOWN", "INVALID_ARGS"}
        observations.append(observation)
        evidence = merge_evidence(evidence, observation.evidence)
        count += 1
        emit(
            runtime,
            "tool_finished",
            call_id=call.call_id,
            tool=call.tool,
            args_json=json.dumps(call.args, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            summary=observation.summary,
            success=observation.success,
            code=observation.code,
            latency_ms=observation.latency_ms or 0,
            audited_by_java=False,
            auto_backstop=auto,
            vault_items=_safe_vault_metadata(observation.data),
        )
        if observation.code in {"UNAVAILABLE", "TIMEOUT"}:
            read_failure = "READ_UNAVAILABLE" if observation.code == "UNAVAILABLE" else "READ_TIMEOUT"
    update = {"observations": observations, "evidence": evidence, "tool_call_count": count,
              "degraded_reasons": list(dict.fromkeys(degraded)), "retry_plan": retry_plan}
    if read_failure and not has_material(replace(state, evidence=evidence, observations=observations)):
        update.update(stop_reason="read_error", error_code=read_failure)
    return update


def observe(state: AgentState) -> dict[str, Any]:
    return {"pending_calls": [], "pending_auto_backstop": []}


def after_observe(state: AgentState, runtime: Runtime[RuntimeContext]) -> str:
    if state.stop_reason in {"read_error", "cancelled", "total_deadline", "write_failed"}:
        return "finalize"
    if state.stop_reason in {"loop_budget", "duplicate_call", "planner_error", "max_rounds"}:
        return "backstop"
    if state.backstop_attempted:
        return "backstop"
    if state.retry_plan:
        return "plan" if state.plan_rounds < runtime.context.max_plan_rounds else "set_max_rounds"
    if state.final_batch:
        return "set_done"
    if state.plan_rounds >= runtime.context.max_plan_rounds:
        return "set_max_rounds"
    try:
        check_control(runtime.context, deadline=runtime.context.loop_deadline)
    except AgentAbort as exc:
        if exc.stop_reason == "loop_budget":
            return "set_loop_budget"
        if exc.stop_reason == "total_deadline":
            return "set_total_deadline"
        raise
    return "plan"


def set_done(state: AgentState) -> dict[str, Any]:
    return {"stop_reason": "done"}


def set_max_rounds(state: AgentState) -> dict[str, Any]:
    return {"stop_reason": "max_rounds"}


def set_loop_budget(state: AgentState) -> dict[str, Any]:
    return {"stop_reason": "loop_budget"}


def set_total_deadline(state: AgentState) -> dict[str, Any]:
    return {"stop_reason": "total_deadline", "error_code": "DEADLINE_EXCEEDED"}


def backstop(state: AgentState, runtime: Runtime[RuntimeContext]) -> dict[str, Any]:
    context = runtime.context
    decision = assess_material(state)
    update = {"backstop_attempted": True, "pending_calls": [], "pending_auto_backstop": [],
              "material_status": decision.status, "material_note": decision.note}
    if decision.status != "read":
        return update
    try:
        check_control(context, deadline=context.loop_deadline)
    except AgentAbort as exc:
        if exc.stop_reason != "loop_budget":
            raise
        update.update(material_status="partial" if _has_body(state) else "missing",
                      material_note="本轮读取预算已用完，未能读完正文；只能依据已有片段分析。",
                      stop_reason="loop_budget")
        return update
    call = ToolCall(call_id=uuid4().hex, tool=decision.tool, args=decision.args or {})
    from agent_core.runner import call_fingerprint

    fingerprint = call_fingerprint(call)
    if fingerprint in state.fingerprints:
        update.update(material_status="partial" if _has_body(state) else "missing",
                      material_note="正文读取没有返回完整内容，暂时不能将这些片段作为全文分析。")
        return update
    update.update(pending_calls=[call], pending_auto_backstop=[True],
                  fingerprints=[*state.fingerprints, fingerprint])
    return update


def _has_body(state: AgentState) -> bool:
    return any(item.text.strip() and item.origin in {"recall_item", "read_record"} for item in state.evidence)


def after_backstop(state: AgentState) -> str:
    if state.pending_calls:
        return "act"
    if state.stop_reason == "read_error":
        return "finalize"
    if state.material_status == "ambiguous":
        return "clarify"
    if state.action_kind == "read" and state.material_status == "missing":
        return "fallback"
    return "synthesize"


def synthesize(state: AgentState, runtime: Runtime[RuntimeContext]) -> dict[str, Any]:
    context = runtime.context
    check_control(context)
    evidence, clipped = answer_evidence(state.evidence)
    index = assign_citations(evidence)
    material_note = state.material_note
    if clipped:
        material_note = "文章较长，本轮上下文只包含部分正文；以下分析仅依据已读取片段，不能代表全文。"
    partial = bool(clipped or state.material_status == "partial" or any(
        item.code in {"UNAVAILABLE", "TIMEOUT"} for item in state.observations
    ))
    messages = answer_service.build_agent_answer_messages(
        state.question,
        state.history_answer,
        evidence,
        state.observations,
        citation_index=index,
        glossary=(state.prepared_context.glossary_matched if state.prepared_context else ()),
        assistant_context=answer_service.assistant_context_text(context.assistant_context, include_divination=True),
    )
    if material_note or partial:
        messages[0]["content"] += "\n材料限制（必须在分析中保留）：" + (material_note or "部分补充工具失败，仅根据已有证据分析。")
    now = context.clock() if context.clock is not None else time.monotonic()
    deadline = min(context.deadline, now + context.answer_budget_ms / 1000.0) if context.deadline is not None else (
        now + context.answer_budget_ms / 1000.0
    )
    timeout = _remaining(deadline, context)
    answer: list[str] = []
    sources = []
    vault_refs = []
    accepted = False
    failure_code = ""
    failure_message = ""
    output_metadata = {}
    if partial:
        prefix = (material_note or "部分补充资料暂时无法读取，以下依据已取得的资料分析。") + "\n\n"
        answer.append(prefix)
        emit(runtime, "delta", prefix)
    stream = answer_service.stream_agent_answer(
        messages,
        evidence,
        state.observations,
        context.llm,
        citation_index=index,
        cancel=lambda: _is_cancelled(context),
        timeout=timeout,
        deadline=deadline,
        clock=context.clock,
    )
    try:
        for event in stream:
            check_control(context, deadline=deadline)
            if event.kind == "thinking":
                emit(runtime, "thinking", event.content)
            elif event.kind == "delta":
                answer.append(event.content)
                emit(runtime, "delta", event.content)
            elif event.kind == "done":
                accepted = True
                sources = event.sources
                vault_refs = event.vault_refs
                output_metadata = event.metadata
            elif event.kind == "failed":
                output_metadata = event.metadata
                failure_code = str(event.metadata.get("code", "OUTPUT_INVALID"))
                failure_message = str(event.metadata.get("message", "未生成有效回答。"))
    finally:
        _close_stream(stream)
    text = "".join(answer)
    output = {"answer_finish_reason": str(output_metadata.get("finish_reason") or ""),
              "answer_repair_count": int(output_metadata.get("repair_count", 0)),
              "prompt_hashes": {**state.prompt_hashes, "agent_chat": hashlib.sha256(messages[0]["content"].encode()).hexdigest()}}
    if not accepted:
        return {**output, "answer": text, "stop_reason": "output_error", "error_code": failure_code or "OUTPUT_EMPTY",
                "error_message": failure_message or "未生成有效回答。", "outcome": "partial_answer" if text else "answer"}
    return {
        **output,
        "answer": text,
        "sources": sources,
        "vault_refs": vault_refs,
        "outcome": "partial_answer" if partial else "answer",
        "material_status": "partial" if clipped else state.material_status,
        "fallback": False,
    }


def fallback(state: AgentState, runtime: Runtime[RuntimeContext]) -> dict[str, Any]:
    context = runtime.context
    check_control(context)
    answer = state.material_note or "我现在没有找到足够的已授权记录来支持具体结论。你可以补充一个时间范围、关键词，或换个更具体的问题，我再帮你查。"
    emit(runtime, "delta", answer)
    return {
        "answer": answer,
        "sources": [],
        "vault_refs": [],
        "fallback": True,
        "outcome": "insufficient_material" if state.material_note else "fallback",
        "stop_reason": state.stop_reason or "no_material",
    }


def clarify(state: AgentState, runtime: Runtime[RuntimeContext]) -> dict[str, Any]:
    context = runtime.context
    check_control(context)
    answer = state.material_note or (state.intent.clarification_question.strip()
              if state.intent and state.intent.clarification_question.strip()
              else "我还差一点信息，你想具体处理哪一项？")
    emit(runtime, "delta", answer)
    return {
        "answer": answer,
        "sources": [],
        "vault_refs": [],
        "stop_reason": "clarification",
        "outcome": "clarification",
        "fallback": False,
    }


def write_disabled(state: AgentState, runtime: Runtime[RuntimeContext]) -> dict[str, Any]:
    return {
        "stop_reason": "write_failed",
        "outcome": "action_failed",
        "error_code": "WRITE_NOT_ENABLED",
    }


def create_record(state: AgentState, runtime: Runtime[RuntimeContext]) -> dict[str, Any]:
    """Execute only Java-authorized original text; the model never supplies saved content."""
    context = runtime.context
    check_control(context)
    if (not isinstance(context.command_grant, str) or not context.command_grant.strip()
            or not isinstance(context.authorized_record_content, str)
            or "create_record" not in context.allowed_write_tools
            or context.command_client is None):
        return write_disabled(state, runtime)

    content = context.authorized_record_content
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    started = time.perf_counter()
    try:
        result = context.command_client.create_record(context, content)
    except Exception:
        from agent_tools.command_client import CommandResult
        from uuid import uuid5, UUID

        result = CommandResult(
            "unknown", "COMMAND_OUTCOME_UNKNOWN",
            str(uuid5(UUID(str(context.run_id)), "create_record:0")),
        )
    latency_ms = max(0, int((time.perf_counter() - started) * 1000))
    emit(
        runtime, "tool_finished", call_id=result.call_id, tool="create_record",
        args_json=json.dumps({"content_sha256": content_hash}, separators=(",", ":")),
        summary=(f"create_record:{result.record_id}" if result.succeeded else result.code),
        success=result.succeeded, code=result.code, latency_ms=latency_ms,
        # A timeout can hide a committed Java write; do not emit a second audit
        # with the same deterministic call ID while the receipt is unknown.
        audited_by_java=result.succeeded or result.outcome == "unknown",
    )
    if result.succeeded:
        answer = "已保存这条日记；目前是仅记录状态"
        emit(runtime, "delta", answer)
        return {
            "answer": answer,
            "sources": [],
            "vault_refs": [],
            "created_records": [CreatedRecord(result.record_id, result.record_status, result.created_at)],
            "outcome": "action_success",
            "stop_reason": "write_completed",
            "fallback": False,
            "tool_call_count": 1,
        }
    if result.outcome == "unknown":
        return {
            "answer": "这次保存结果暂时无法核对，我不能确认是否已创建。请稍后查看本轮状态。",
            "sources": [],
            "vault_refs": [],
            "created_records": [],
            "outcome": "action_unknown",
            "stop_reason": "write_unknown",
            "fallback": False,
            "error_code": result.code,
            "tool_call_count": 1,
        }
    return {
        "answer": "",
        "sources": [],
        "vault_refs": [],
        "created_records": [],
        "outcome": "action_failed",
        "stop_reason": "write_failed",
        "fallback": False,
        "error_code": result.code,
        "tool_call_count": 1,
    }


def finalize(state: AgentState, runtime: Runtime[RuntimeContext]) -> dict[str, Any]:
    context = runtime.context
    tools_used = list(dict.fromkeys(item.tool for item in state.observations))
    if state.action_kind == "create_record" and state.tool_call_count and "create_record" not in tools_used:
        tools_used.append("create_record")
    if state.action_kind == "prepare_action" and state.intent and state.intent.prepare_tool:
        tools_used.append(state.intent.prepare_tool)
    emit(runtime, "meta", route=state.route, tools_used=tools_used)
    if state.stop_reason in {"cancelled", "total_deadline", "read_error", "write_failed", "output_error"}:
        emit(
            runtime,
            "failed",
            code=state.error_code or state.stop_reason.upper(),
            message=state.error_message or _failure_message(state.stop_reason),
            has_partial_answer=bool(state.answer),
            stop_reason=state.stop_reason,
            plan_rounds=state.plan_rounds,
            tool_call_count=state.tool_call_count,
            metrics=_metrics(state),
        )
        return {}

    if not state.stop_reason:
        state_reason = "done"
    else:
        state_reason = state.stop_reason
    prepared = state.prepared_context
    emit(
        runtime,
        "completed",
        answer_text=state.answer,
        sources=state.sources,
        vault_refs=state.vault_refs,
        created_records=state.created_records,
        stop_reason=state_reason,
        outcome=state.outcome or "answer",
        fallback=state.fallback,
        glossary_hit_ids=list(prepared.matched_ids) if prepared is not None else [],
        plan_rounds=state.plan_rounds,
        tool_call_count=state.tool_call_count,
        metrics=_metrics(state),
    )
    return {"stop_reason": state_reason}


def _candidate_ids(observations) -> list[int]:
    ids = []
    for observation in observations:
        if not observation.success or observation.tool != "find_item" or not isinstance(observation.data, dict):
            continue
        for item in observation.data.get("items", ()) or ():
            if isinstance(item, dict) and isinstance(item.get("vault_item_id"), int):
                ids.append(item["vault_item_id"])
    return list(dict.fromkeys(ids))


def _safe_vault_metadata(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    candidates = list(data.get("items", ()) or ())
    if isinstance(data.get("item"), dict):
        candidates.append(data["item"])
    safe = []
    for item in candidates:
        if not isinstance(item, dict) or item.get("vault_item_id") is None:
            continue
        safe.append({
            "vault_item_id": item.get("vault_item_id"),
            "original_name": item.get("original_name", ""),
            "category": item.get("category", ""),
            "size_bytes": item.get("size_bytes", 0),
            "digest_status": item.get("digest_status", ""),
        })
    return safe


def _metrics(state: AgentState) -> dict[str, Any]:
    return {"degraded_reasons": list(dict.fromkeys(state.degraded_reasons)),
            "prompt_hashes": state.prompt_hashes,
            "diagnostics": {"planning_protocol": state.planning_protocol,
                            "plan_finish_reason": state.plan_finish_reason,
                            "answer_finish_reason": state.answer_finish_reason,
                            "answer_repair_count": state.answer_repair_count,
                            "material_status": state.material_status,
                            "evidence_chars": sum(len(item.text) for item in state.evidence),
                            "body_pages": sum(item.tool in {"recall_item", "read_record"} for item in state.observations),
                            "answer_chars": len(state.answer)}}


def emit(runtime: Runtime[RuntimeContext], kind: str, content: str = "", **metadata: Any) -> None:
    context = runtime.context
    event = {"kind": kind, "content": content, "metadata": metadata}
    if callable(context.event_writer):
        context.event_writer(event)
        return
    get_stream_writer()(event)


def _is_cancelled(context: RuntimeContext) -> bool:
    cancelled = context.cancelled
    if cancelled is None:
        return False
    try:
        return bool(cancelled() if callable(cancelled) else cancelled.is_set())
    except Exception:
        return True


def _phase_deadline(context: RuntimeContext, phase_deadline: float | None) -> float | None:
    values = [value for value in (context.deadline, phase_deadline) if value is not None]
    return min(values) if values else None


def _remaining(deadline: float | None, context: RuntimeContext) -> float | None:
    if deadline is None:
        return None
    remaining = deadline - (context.clock() if context.clock is not None else time.monotonic())
    if remaining <= 0:
        check_control(context, deadline=deadline)
    return remaining


def _with_effective_deadline(context: RuntimeContext, phase_deadline: float | None) -> RuntimeContext:
    deadline = _phase_deadline(context, phase_deadline)
    return replace(context, deadline=deadline)


def _close_stream(stream: Any) -> None:
    close = getattr(stream, "close", None)
    if callable(close):
        close()


def _failure_message(stop_reason: str) -> str:
    if stop_reason == "cancelled":
        return "请求已取消"
    if stop_reason == "total_deadline":
        return "本轮处理超时"
    if stop_reason == "read_error":
        return "读取授权数据时发生错误"
    return "本轮写入能力尚未启用"
