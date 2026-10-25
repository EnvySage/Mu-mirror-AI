"""Intent extraction shared by the legacy servicer and future agent flows."""

from dataclasses import dataclass, replace
from datetime import datetime
import re
from types import SimpleNamespace
from typing import Any, Callable
import time

from agent_core.time_ranges import normalize_time
from agent_core.types import Clarification
from agent_core.types import IntentResult as AgentIntentResult
from agent_core.types import PreparedContext, RuntimeContext, TimeExpression
from mirror_core.glossary_render import format_glossary
from llm.factory import create_llm
from mirror_core.llm_json import parse_json
from mirror_core.prompts_loader import loader

QUERY_TYPES = {"profile", "structured", "semantic", "hybrid"}
DEFAULT_QUERY_TYPE = "hybrid"
CONTENT_TYPES = {"todo", "thought", "learning", "plan", "note", "work", "social", "health"}
MOODS = {"happy", "excited", "satisfied", "grateful", "expecting", "calm", "bored",
         "confused", "anxious", "sad", "angry", "exhausted", "stressed"}


@dataclass(frozen=True)
class IntentResult:
    query_type: str
    content_type: Any
    moods: list[str]
    time_range: Any
    rewritten_query: Any


def extract_intent(query, glossary, llm_config, *, llm_factory: Callable = create_llm) -> IntentResult:
    """Run the existing intent prompt and apply the legacy field validation."""
    print(f"[ExtractIntent] query: {query}")
    print(f"[ExtractIntent] LLM: provider={llm_config.provider}, model={llm_config.model}")

    llm = llm_factory(
        provider=llm_config.provider,
        api_key=llm_config.api_key,
        base_url=llm_config.base_url,
        model=llm_config.model,
        protocol=llm_config.protocol,
    )
    prompt = loader.render(
        "intent",
        query=query,
        glossary=format_glossary(glossary),
    )
    response_text = llm.json_task([{"role": "user", "content": prompt}])
    print(f"[ExtractIntent] LLM 响应: {response_text[:200]}")

    result = parse_json(response_text, ctx="意图")
    content_type = result.get("content_type")
    return IntentResult(
        query_type=(result.get("query_type")
                    if result.get("query_type") in QUERY_TYPES else DEFAULT_QUERY_TYPE),
        content_type=content_type if content_type in CONTENT_TYPES else None,
        moods=[m for m in result.get("moods", []) if m in MOODS],
        time_range=result.get("time_range", ""),
        rewritten_query=result.get("rewritten_query") or query,
    )


AGENT_ACTION_KINDS = {"read", "create_record", "prepare_action", "conversation", "clarification"}
AGENT_FILE_QUERY_MODES = {"none", "list", "body"}
_TIME_EXPRESSION_FIELDS = {"kind", "date", "month", "offset", "days"}


def understand(
    question: str,
    prepared: PreparedContext,
    runtime: RuntimeContext,
    *,
    llm_factory: Callable | None = None,
) -> AgentIntentResult:
    """Understand one graph_v2 question while keeping write authority in trusted runtime data."""
    if not isinstance(question, str):
        raise TypeError("question must be a string")

    if _has_trusted_create_grant(runtime):
        return AgentIntentResult(
            action_kind="create_record",
            route="structured",
            standalone_question=question,
            content_type=None,
            moods=(),
            time_expression=TimeExpression(kind="none"),
            file_query_mode="none",
        )

    llm = getattr(runtime, "llm", None)
    if llm is None:
        config = getattr(runtime, "llm_config", None)
        if config is None:
            raise ValueError("graph_v2 intent requires an LLM client or request LLM config")
        factory = llm_factory or create_llm
        timeout = _remaining_timeout(getattr(runtime, "deadline", None), getattr(runtime, "clock", None))
        llm = factory(
            provider=config.provider,
            api_key=config.api_key,
            base_url=config.base_url,
            model=config.model,
            protocol=config.protocol,
            timeout=timeout,
            max_retries=0,
        )

    history = "\n".join(
        f"{message.role}: {message.content}" for message in prepared.history_plan
    )
    prompt = loader.render(
        "agent_intent",
        question=question,
        history=history,
        glossary=_format_agent_glossary(prepared.glossary_for_intent),
        prepare_tools=_format_prepare_tools(getattr(runtime, "prepare_tools", ())),
    )
    timeout = _remaining_timeout(getattr(runtime, "deadline", None), getattr(runtime, "clock", None))
    # Card parameters can contain up to 2000 UTF-16 units of diary text. The old
    # 256-token routing budget cannot hold even the expanded intent envelope.
    json_options = {"max_tokens": 8192} if getattr(runtime, "prepare_tools", ()) else {}
    response = parse_json(
        llm.json_task(
            [{"role": "user", "content": prompt}], timeout=timeout, max_retries=0,
            **json_options,
        ),
        ctx="新版意图",
    )
    result = _validate_agent_intent(response, question, runtime)
    if result.action_kind == "clarification":
        return result
    resolved_question = _resolve_confirmed_glossary_aliases(
        question, result.standalone_question, prepared.glossary_matched,
    )
    if resolved_question != result.standalone_question:
        result = replace(result, standalone_question=resolved_question)
    normalized = normalize_time(result.time_expression, runtime.reference_date, runtime.timezone)
    if isinstance(normalized, Clarification):
        return AgentIntentResult(
            action_kind="clarification", route=result.route,
            standalone_question=result.standalone_question, content_type=result.content_type,
            moods=result.moods, time_expression=result.time_expression,
            file_query_mode=result.file_query_mode,
            validation_issues=tuple(dict.fromkeys((*result.validation_issues, normalized.code))),
            prepare_tool=result.prepare_tool,
            prepare_args=result.prepare_args,
            clarification_question=result.clarification_question,
        )
    return AgentIntentResult(
        action_kind=result.action_kind, route=result.route,
        standalone_question=result.standalone_question, content_type=result.content_type,
        moods=result.moods, time_expression=result.time_expression,
        file_query_mode=result.file_query_mode, validation_issues=result.validation_issues,
        time_window=normalized,
        prepare_tool=result.prepare_tool,
        prepare_args=result.prepare_args,
        clarification_question=result.clarification_question,
    )


def _format_prepare_tools(tools) -> str:
    rows = []
    for tool in tools or ():
        if not isinstance(tool, dict):
            continue
        name = tool.get("name", "")
        description = tool.get("description", "")
        args_schema = tool.get("args_schema", "")
        if name and description:
            rows.append(f"- {name}: {description} 参数：{args_schema}")
    return "\n".join(rows) if rows else "（当前没有可准备的操作卡工具）"


def _resolve_confirmed_glossary_aliases(question: str, standalone_question: str, terms) -> str:
    """Normalize unique, confirmed aliases before semantic retrieval and planning."""
    question_folded = question.casefold()
    aliases: dict[str, tuple[str, str]] = {}
    ambiguous: set[str] = set()
    for term in terms or ():
        canonical = getattr(term, "term", "")
        if not isinstance(canonical, str) or not canonical.strip():
            continue
        for alias in getattr(term, "aliases", ()) or ():
            if not isinstance(alias, str) or not alias.strip() or alias.casefold() not in question_folded:
                continue
            key = alias.casefold()
            current = aliases.get(key)
            if current is not None and current[1] != canonical:
                ambiguous.add(key)
            else:
                aliases[key] = (alias, canonical)

    resolved = standalone_question
    seen_terms: set[str] = set()
    for key, (alias, canonical) in sorted(aliases.items(), key=lambda item: len(item[1][0]), reverse=True):
        if key in ambiguous or canonical in seen_terms:
            continue
        seen_terms.add(canonical)
        pattern = re.compile(re.escape(alias), re.IGNORECASE)
        if pattern.search(resolved):
            resolved = pattern.sub(canonical, resolved)
        elif canonical.casefold() not in resolved.casefold():
            resolved = f"{resolved}（已确认词条：{canonical}）"
    return resolved


def _remaining_timeout(deadline: float | None, clock: Callable[[], float] | None = None) -> float | None:
    if deadline is None:
        return None
    remaining = float(deadline) - (clock() if clock is not None else time.monotonic())
    if remaining <= 0:
        raise TimeoutError("graph_v2 deadline expired before the LLM request")
    return remaining


def _has_trusted_create_grant(runtime: RuntimeContext) -> bool:
    grant = getattr(runtime, "command_grant", None)
    content = getattr(runtime, "authorized_record_content", None)
    allowed_tools = getattr(runtime, "allowed_write_tools", ()) or ()
    return (
        isinstance(grant, str)
        and bool(grant.strip())
        and isinstance(content, str)
        and bool(content.strip())
        and isinstance(allowed_tools, (tuple, list, set, frozenset))
        and "create_record" in allowed_tools
    )


def _format_agent_glossary(terms) -> str:
    """Adapt the read-model timestamp name to the established glossary renderer."""
    adapted = []
    for term in terms:
        confirmed_at = getattr(term, "last_confirmed_at", None)
        if isinstance(confirmed_at, datetime):
            confirmed_at = confirmed_at.isoformat()
        adapted.append(SimpleNamespace(
            term=term.term,
            aliases=term.aliases,
            description=term.description,
            confirmed_at=confirmed_at or "",
        ))
    return format_glossary(adapted)


def _validate_agent_intent(
    result: dict[str, Any], question: str, runtime: RuntimeContext
) -> AgentIntentResult:
    issues: list[str] = []
    action_kind = result.get("action_kind")
    if not isinstance(action_kind, str) or action_kind not in AGENT_ACTION_KINDS:
        action_kind = "clarification"
        issues.append("invalid_action_kind")

    route = result.get("route")
    if not isinstance(route, str) or route not in QUERY_TYPES:
        route = DEFAULT_QUERY_TYPE
        issues.append("invalid_route")

    standalone_question = result.get("standalone_question")
    if not isinstance(standalone_question, str) or not standalone_question.strip():
        standalone_question = question
        issues.append("invalid_standalone_question")

    content_type = result.get("content_type")
    if not isinstance(content_type, str) or content_type not in CONTENT_TYPES:
        if content_type is not None:
            issues.append("invalid_content_type")
        content_type = None

    raw_moods = result.get("moods", [])
    if not isinstance(raw_moods, list):
        raw_moods = []
        issues.append("invalid_moods")
    moods = tuple(dict.fromkeys(mood for mood in raw_moods if isinstance(mood, str) and mood in MOODS))
    if len(moods) != len(raw_moods):
        issues.append("invalid_moods")

    expression, time_issue = _parse_agent_time_expression(result.get("time_expression"))
    if time_issue:
        issues.append(time_issue)

    file_query_mode = result.get("file_query_mode", "none")
    if not isinstance(file_query_mode, str) or file_query_mode not in AGENT_FILE_QUERY_MODES:
        file_query_mode = "none"
        issues.append("invalid_file_query_mode")

    prepare_tool = result.get("prepare_tool")
    prepare_args = result.get("prepare_args")
    clarification_question = result.get("clarification_question", "")
    if not isinstance(clarification_question, str) or len(clarification_question) > 160:
        clarification_question = ""
        issues.append("invalid_clarification_question")
    if action_kind == "prepare_action":
        available = {
            item.get("name") for item in (getattr(runtime, "prepare_tools", ()) or ())
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
        if not isinstance(prepare_tool, str) or prepare_tool not in available:
            prepare_tool = None
            issues.append("unavailable_prepare_tool")
        if not isinstance(prepare_args, dict):
            prepare_args = None
            issues.append("invalid_prepare_args")
        elif len(str(prepare_args)) > 12000:
            prepare_args = None
            issues.append("prepare_args_too_large")
    else:
        prepare_tool = None
        prepare_args = None

    if action_kind == "create_record" and not _has_trusted_create_grant(runtime):
        action_kind = "clarification"
        issues.append("unauthorized_create_record")

    if issues and action_kind not in {"clarification", "create_record"}:
        action_kind = "clarification"

    return AgentIntentResult(
        action_kind=action_kind,
        route=route,
        standalone_question=standalone_question,
        content_type=content_type,
        moods=moods,
        time_expression=expression,
        file_query_mode=file_query_mode,
        validation_issues=tuple(dict.fromkeys(issues)),
        prepare_tool=prepare_tool,
        prepare_args=prepare_args,
        clarification_question=clarification_question.strip(),
    )


def _parse_agent_time_expression(value: Any) -> tuple[TimeExpression, str | None]:
    if not isinstance(value, dict) or set(value) - _TIME_EXPRESSION_FIELDS:
        return TimeExpression(kind="none"), "invalid_time_expression"
    kind = value.get("kind")
    if not isinstance(kind, str) or kind not in {"none", "day", "month", "calendar_week", "calendar_month", "rolling_days"}:
        return TimeExpression(kind="none"), "invalid_time_expression"
    date_value = value.get("date")
    month_value = value.get("month")
    if (date_value is not None and not isinstance(date_value, str)) or (
        month_value is not None and not isinstance(month_value, str)
    ):
        return TimeExpression(kind="none"), "invalid_time_expression"
    offset = value.get("offset")
    days = value.get("days")
    if (offset is not None and (isinstance(offset, bool) or not isinstance(offset, int))) or (
        days is not None and (isinstance(days, bool) or not isinstance(days, int))
    ):
        return TimeExpression(kind="none"), "invalid_time_expression"
    allowed_fields = {
        "none": set(),
        "day": {"date", "offset"},
        "month": {"month", "offset"},
        "calendar_week": {"offset"},
        "calendar_month": {"offset"},
        "rolling_days": {"days", "offset"},
    }[kind]
    supplied_fields = {
        name for name, field_value in (
            ("date", date_value), ("month", month_value), ("offset", offset), ("days", days)
        ) if field_value is not None
    }
    if supplied_fields - allowed_fields:
        return TimeExpression(kind="none"), "invalid_time_expression"
    return TimeExpression(kind=kind, date=date_value, month=month_value, offset=offset, days=days), None
