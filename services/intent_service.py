"""Intent extraction shared by the legacy servicer and future agent flows."""

from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Callable
import time

from agent_core.time_ranges import normalize_time
from agent_core.types import Clarification
from agent_core.types import IntentResult as AgentIntentResult
from agent_core.types import PreparedContext, RuntimeContext, TimeExpression
from glossary_render import format_glossary
from llm.factory import create_llm
from llm_json import parse_json
from prompts_loader import loader

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


AGENT_ACTION_KINDS = {"read", "create_record", "conversation", "clarification"}
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
        timeout = _remaining_timeout(getattr(runtime, "deadline", None))
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
    )
    timeout = _remaining_timeout(getattr(runtime, "deadline", None))
    response = parse_json(
        llm.json_task(
            [{"role": "user", "content": prompt}], timeout=timeout, max_retries=0,
        ),
        ctx="新版意图",
    )
    result = _validate_agent_intent(response, question, runtime)
    if result.action_kind == "clarification":
        return result
    normalized = normalize_time(result.time_expression, runtime.reference_date, runtime.timezone)
    if isinstance(normalized, Clarification):
        return AgentIntentResult(
            action_kind="clarification", route=result.route,
            standalone_question=result.standalone_question, content_type=result.content_type,
            moods=result.moods, time_expression=result.time_expression,
            file_query_mode=result.file_query_mode,
            validation_issues=tuple(dict.fromkeys((*result.validation_issues, normalized.code))),
        )
    return AgentIntentResult(
        action_kind=result.action_kind, route=result.route,
        standalone_question=result.standalone_question, content_type=result.content_type,
        moods=result.moods, time_expression=result.time_expression,
        file_query_mode=result.file_query_mode, validation_issues=result.validation_issues,
        time_window=normalized,
    )


def _remaining_timeout(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    remaining = float(deadline) - time.monotonic()
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
