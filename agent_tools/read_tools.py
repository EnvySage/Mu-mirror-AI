"""Execution adapters for the nine graph_v2 read-only tools."""

from __future__ import annotations

import re
import time
from datetime import date, datetime, time as day_time, timedelta
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from agent_core.types import Evidence, RuntimeContext, ToolCall, ToolObservation
from agent_data.glossary import GlossaryRepository
from agent_data.history import HistoryRepository
from agent_data.profiles import ProfileRepository
from agent_data.records import CONTENT_TYPES, MOODS, RecordFilters, RecordRepository, RecordWindow
from agent_data.scope import ReadScope, bounded_limit, reject_scope_overrides, require_scope
from agent_data.todos import TodoRepository
from agent_data.vault import VaultFilters, VaultRepository
from agent_tools.registry import READ_TOOL_REGISTRY


_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_DIMENSIONS = (
    ("mood", "mood_analysis"),
    ("learning", "learning_analysis"),
    ("todo", "todo_analysis"),
    ("rhythm", "rhythm_analysis"),
    ("summary", "overall_summary"),
)


def execute_read_tool(call: ToolCall, runtime: RuntimeContext) -> ToolObservation:
    started = time.perf_counter()
    call_id = getattr(call, "call_id", "") if call is not None else ""
    tool = getattr(call, "tool", "") if call is not None else ""
    try:
        if not isinstance(call, ToolCall):
            raise ValueError("tool call is malformed")
        if not isinstance(runtime, RuntimeContext):
            raise ValueError("runtime context is missing")
        spec = READ_TOOL_REGISTRY.get(call.tool)
        if spec is None:
            return _observation(call_id, tool, False, "UNKNOWN", "未知或未授权的只读工具", latency=started)
        args = _validate_args(call.args)
        repos = _repositories(runtime)
        scope = require_scope(runtime.read_scope)
        handler = _HANDLERS[call.tool]
        success, code, summary, data, evidence, has_material = handler(args, runtime, scope, repos)
        return _observation(call_id, tool, success, code, summary, data, evidence, has_material, started)
    except (TypeError, ValueError) as exc:
        return _observation(call_id, tool, False, "INVALID_ARGS", str(exc), latency=started)
    except TimeoutError as exc:
        return _observation(call_id, tool, False, "TIMEOUT", str(exc), latency=started)
    except Exception as exc:
        return _observation(
            call_id, tool, False, "UNAVAILABLE", f"只读数据暂不可用（{type(exc).__name__}）", latency=started,
        )


def _validate_args(args: Any) -> dict[str, Any]:
    if args is None:
        args = {}
    if not isinstance(args, Mapping):
        raise ValueError("tool arguments must be an object")
    reject_scope_overrides(args)
    return dict(args)


def _repositories(runtime: RuntimeContext):
    repos = getattr(runtime, "repositories", None)
    if repos is not None:
        return repos
    pool = getattr(runtime, "read_pool", None)
    if pool is None:
        raise ValueError("read-only repository context is missing")
    from types import SimpleNamespace
    return SimpleNamespace(
        records=RecordRepository(pool), todos=TodoRepository(pool), profiles=ProfileRepository(pool),
        glossary=GlossaryRepository(pool), history=HistoryRepository(pool), vault=VaultRepository(pool),
    )


def _common(args: dict[str, Any], runtime: RuntimeContext, *, allow: set[str]) -> dict[str, Any]:
    unknown = set(args) - allow
    if unknown:
        raise ValueError(f"unsupported argument(s): {', '.join(sorted(unknown))}")
    return args


def _date_bounds(args: dict[str, Any], runtime: RuntimeContext) -> tuple[datetime | None, datetime | None]:
    present = [name for name in ("date", "date_from", "date_to", "days") if name in args]
    if "date" in args and len(present) != 1:
        raise ValueError("date cannot be combined with another date range")
    if "days" in args and len(present) != 1:
        raise ValueError("days cannot be combined with an explicit date range")
    zone = ZoneInfo(runtime.timezone)
    if not present:
        return None, None
    if "days" in args:
        days = _positive_int(args["days"], "days", maximum=36500)
        end_day = runtime.reference_date + timedelta(days=1)
        start_day = end_day - timedelta(days=days)
        return _at_start(start_day, zone), _at_start(end_day, zone)
    if "date" in args:
        start_day = _parse_date(args["date"], "date")
        return _at_start(start_day, zone), _at_start(start_day + timedelta(days=1), zone)
    start_day = _parse_date(args["date_from"], "date_from") if args.get("date_from") else None
    end_day = _parse_date(args["date_to"], "date_to") if args.get("date_to") else None
    if start_day is not None and end_day is not None and start_day > end_day:
        raise ValueError("date_from must be on or before date_to")
    return (
        _at_start(start_day, zone) if start_day is not None else None,
        _at_start(end_day + timedelta(days=1), zone) if end_day is not None else None,
    )


def _at_start(day: date, zone: ZoneInfo) -> datetime:
    return datetime.combine(day, day_time.min, tzinfo=zone)


def _parse_date(value: Any, name: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a valid YYYY-MM-DD date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{name} must be YYYY-MM-DD")
    return parsed


def _positive_int(value: Any, name: str, *, maximum: int = 2_147_483_647) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return min(value, maximum)


def _search_records(args, runtime, scope, repos):
    _common(args, runtime, allow={"date", "date_from", "date_to", "days", "moods", "content_type", "query", "limit"})
    start_at, end_at = _date_bounds(args, runtime)
    content_type = args.get("content_type")
    if content_type is not None and content_type not in CONTENT_TYPES:
        raise ValueError("content_type is not supported")
    moods = args.get("moods", ())
    if not isinstance(moods, (list, tuple)) or isinstance(moods, str):
        raise ValueError("moods must be an array")
    moods = tuple(dict.fromkeys(moods))
    if any(not isinstance(mood, str) or mood not in MOODS for mood in moods):
        raise ValueError("moods contains an unsupported value")
    query = args.get("query")
    if query is not None and (not isinstance(query, str) or len(query.strip()) > 256):
        raise ValueError("query must contain at most 256 characters")
    limit = bounded_limit(args.get("limit", 20), maximum=20, default=20)
    filters = RecordFilters(
        keyword=query.strip() if isinstance(query, str) and query.strip() else None,
        content_type=content_type,
        moods=moods,
        start_at=start_at,
        end_at=end_at,
    )
    chunks = repos.records.search(scope, filters, limit=limit)
    evidence = tuple(_chunk_evidence(chunk, "search_records") for chunk in chunks)
    records = [
        {
            "chunk_id": chunk.chunk_id,
            "record_id": chunk.record_id,
            "date": chunk.created_at.isoformat(),
            "content_type": chunk.metadata.get("contentType", ""),
            "moods": chunk.metadata.get("mood", []),
            "title": chunk.metadata.get("title", ""),
            "quote": chunk.segment,
        }
        for chunk in chunks
    ]
    summary = f"找到 {len(records)} 段已授权日记片段" + ("（结果可能截断）" if len(records) == 20 else "")
    return True, "OK" if records else "EMPTY", summary, {"records": records, "truncated": len(records) == 20}, evidence, bool(records)


def _get_stats(args, runtime, scope, repos):
    _common(args, runtime, allow={"date", "date_from", "date_to", "days"})
    start_at, end_at = _date_bounds(args, runtime)
    window = RecordWindow(start_at, end_at)
    stats = repos.records.stats(scope, window)
    todos = repos.todos.stats(scope)
    data = {
        "records": {"count": stats.record_count, "moods": dict(stats.mood_counts)},
        "record_window": {"start": _iso(stats.start_at), "end_exclusive": _iso(stats.end_at)},
        "todos": {"count": todos.total, "by_status": dict(todos.by_status)},
        "todo_window": "all currently authorized todos (not date filtered)",
    }
    return True, "OK", f"统计窗口内记录 {stats.record_count} 条，待办 {todos.total} 项", data, (), True


def _get_profile(args, runtime, scope, repos):
    _common(args, runtime, allow={"month"})
    month = args.get("month")
    if month is not None and (not isinstance(month, str) or not _MONTH.fullmatch(month)):
        raise ValueError("month must use YYYY-MM")
    profiles = repos.profiles.recent(scope, limit=2, month=month)
    if not profiles:
        return True, "EMPTY", "没有可用的画像快照", {"available": False, "month": month}, (), False
    profile = profiles[0]
    data = {"available": True, "month": month, **_profile_data(profile)}
    evidence = (_profile_evidence(profile),)
    return True, "OK", f"读取到 {profile.snapshot_type} 画像快照", data, evidence, True


def _get_coverage(args, runtime, scope, repos):
    _common(args, runtime, allow={"keyword"})
    keyword = args.get("keyword")
    if not isinstance(keyword, str) or not keyword.strip() or len(keyword.strip()) > 256:
        raise ValueError("keyword must contain 1 to 256 characters")
    coverage = repos.records.coverage(scope, keyword.strip())
    data = {
        "keyword": keyword.strip(),
        "first_date": coverage.first_date.isoformat() if coverage.first_date else None,
        "latest_date": coverage.latest_date.isoformat() if coverage.latest_date else None,
        "distinct_dates": coverage.distinct_dates,
        "record_count": coverage.record_count,
    }
    return True, "OK" if coverage.record_count else "EMPTY", f"主题命中 {coverage.distinct_dates} 个不同日期", data, (), bool(coverage.record_count)


def _get_glossary(args, runtime, scope, repos):
    _common(args, runtime, allow=set())
    terms = list(repos.glossary.top_confirmed(scope, limit=30))
    data = {"terms": [
        {"id": term.id, "term": term.term, "aliases": list(term.aliases), "description": term.description,
         "query_hit_count": term.query_hit_count}
        for term in terms
    ]}
    return True, "OK" if terms else "EMPTY", f"已确认词典 {len(terms)} 项", data, (), bool(terms)


def _compare_snapshots(args, runtime, scope, repos):
    _common(args, runtime, allow={"first_id", "second_id"})
    first_id = _positive_int(args.get("first_id"), "first_id")
    second_id = _positive_int(args.get("second_id"), "second_id")
    if first_id == second_id:
        raise ValueError("two different profile ids are required")
    compared = repos.profiles.compare(scope, first_id, second_id)
    if compared is None:
        return False, "NOT_FOUND", "没有找到两份属于当前用户的有效快照", None, (), False
    first, second = compared
    data = {"first": _profile_data(first), "second": _profile_data(second)}
    evidence = (_profile_evidence(first), _profile_evidence(second))
    return True, "OK", "已读取两份有效快照供比较", data, evidence, True


def _find_item(args, runtime, scope, repos):
    _common(args, runtime, allow={"query", "category", "date_from", "date_to", "limit"})
    query = args.get("query")
    if query is not None and (not isinstance(query, str) or len(query.strip()) > 256):
        raise ValueError("query must contain at most 256 characters")
    normalized_query = query.strip() if isinstance(query, str) and query.strip() else None
    category = args.get("category")
    if category is not None and category not in CONTENT_TYPES:
        raise ValueError("category is not supported")
    start_at, end_at = _date_bounds({k: v for k, v in args.items() if k in {"date_from", "date_to"}}, runtime)
    limit = bounded_limit(args.get("limit", 10), maximum=10, default=10)
    aliases = ()
    if normalized_query:
        folded = normalized_query.casefold()
        terms = repos.glossary.top_confirmed(scope, limit=30)
        aliases = tuple(dict.fromkeys(
            word
            for term in terms
            if any(value.casefold() in folded or folded in value.casefold() for value in (term.term, *term.aliases))
            for word in term.aliases if isinstance(word, str) and word.strip()
        ))
    filters = VaultFilters(category=category, created_after=start_at, created_before=end_at, aliases=aliases)
    timeout = _remaining_timeout(runtime.deadline)
    embedder = runtime.embedding_client
    degraded = bool(normalized_query and embedder is None)
    try:
        items = repos.vault.find(
            scope, normalized_query, filters, embedder,
            limit=limit, timeout=timeout, max_retries=0,
        )
    except TimeoutError:
        raise
    except Exception:
        degraded = True
        items = repos.vault.find(
            scope, normalized_query, filters, None,
            limit=limit,
        )
    payload = [_vault_item_data(item) for item in items]
    evidence = tuple(_vault_item_evidence(item, "find_item") for item in items)
    summary = f"找到 {len(items)} 个文件候选"
    if degraded:
        summary += "；向量检索不可用，仅保留名称/描述与元数据结果"
    return True, "OK" if items else "EMPTY", summary, {"items": payload, "count": len(items), "degraded": degraded}, evidence, bool(items)


def _recall_item(args, runtime, scope, repos):
    _common(args, runtime, allow={"vault_item_id", "query"})
    item_id = _positive_int(args.get("vault_item_id"), "vault_item_id")
    query = args.get("query")
    if query is not None and (not isinstance(query, str) or len(query.strip()) > 256):
        raise ValueError("query must contain at most 256 characters")
    timeout = _remaining_timeout(runtime.deadline)
    recalled = repos.vault.recall(
        scope, item_id, query.strip() if isinstance(query, str) and query.strip() else None,
        runtime.embedding_client, timeout=timeout, max_retries=0,
    )
    if recalled is None:
        return False, "NOT_FOUND", "文件不存在或不属于当前用户", None, (), False
    item = recalled.item
    snippets = tuple(recalled.snippets)
    data = {
        "item": _vault_item_data(item),
        "snippets": [
            {"chunk_id": snippet.chunk_id, "record_id": snippet.record_id,
             "date": snippet.created_at.isoformat(), "text": snippet.text, "distance": snippet.distance}
            for snippet in snippets
        ],
        "body_available": item.digest_status == "confirmed",
    }
    evidence = tuple(_vault_snippet_evidence(item, snippet) for snippet in snippets)
    if item.digest_status != "confirmed":
        return True, "EMPTY", "文件元信息存在，但内容尚未确认；没有读取正文", data, (), False
    return True, "OK" if snippets else "EMPTY", f"已读取 {len(snippets)} 个已确认正文片段", data, evidence, bool(snippets)


def _save_item(args, runtime, scope, repos):
    _common(args, runtime, allow={"vault_item_id"})
    item_id = _positive_int(args.get("vault_item_id"), "vault_item_id")
    item = repos.vault.metadata(scope, item_id)
    if item is None:
        return False, "NOT_FOUND", "文件不存在或不属于当前用户", {"saved": False}, (), False
    return True, "OK", f"已保存文件：{item.original_name}", {
        "saved": True, "item": _vault_item_data(item),
    }, (_vault_item_evidence(item, "save_item"),), True


_HANDLERS = {
    "search_records": _search_records,
    "get_stats": _get_stats,
    "get_profile": _get_profile,
    "get_coverage": _get_coverage,
    "get_glossary": _get_glossary,
    "compare_snapshots": _compare_snapshots,
    "find_item": _find_item,
    "recall_item": _recall_item,
    "save_item": _save_item,
}


def _chunk_evidence(chunk, origin: str) -> Evidence:
    return Evidence(
        evidence_id=f"chunk:{chunk.chunk_id}", kind="diary", record_id=chunk.record_id,
        chunk_id=chunk.chunk_id, text=chunk.segment,
        date=chunk.created_at.isoformat() if chunk.created_at else None,
        distance=getattr(chunk, "distance", None), origin=origin,
        ranking_score=getattr(chunk, "ranking_score", None),
    )


def _profile_data(profile):
    return {
        "id": profile.id,
        "snapshot_type": profile.snapshot_type,
        "period_month": profile.period_month,
        "created_at": _iso(profile.created_at),
        "dimensions": {name: getattr(profile, attribute) for name, attribute in _DIMENSIONS},
        "user_tags": profile.user_tags,
    }


def _profile_evidence(profile):
    dimensions = _profile_data(profile)["dimensions"]
    text = "\n".join(f"{key}: {value}" for key, value in dimensions.items() if value)
    return Evidence(evidence_id=f"profile:{profile.id}", kind="profile", text=text,
                    date=_iso(profile.created_at), origin="profile")


def _vault_item_data(item):
    return {
        "vault_item_id": item.id, "original_name": item.original_name, "mime": item.mime,
        "size_bytes": item.size_bytes, "category": item.category, "description": item.description,
        "digest_status": item.digest_status, "created_at": _iso(item.created_at),
        "match_layer": item.match_layer, "distance": item.distance,
    }


def _vault_item_evidence(item, origin):
    return Evidence(
        evidence_id=f"vault:{item.id}", kind="vault", vault_item_id=item.id,
        text=f"{item.original_name}: {item.description or ''}".strip(),
        date=_iso(item.created_at), distance=item.distance, origin=origin,
    )


def _vault_snippet_evidence(item, snippet):
    return Evidence(
        evidence_id=f"vault:{item.id}:chunk:{snippet.chunk_id}", kind="vault",
        record_id=snippet.record_id, chunk_id=snippet.chunk_id, vault_item_id=item.id,
        text=snippet.text, date=_iso(snippet.created_at), distance=snippet.distance,
        origin="recall_item",
    )


def _iso(value):
    return value.isoformat() if value is not None else None


def _remaining_timeout(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("graph_v2 deadline expired before data query")
    return remaining


def _observation(call_id, tool, success, code, summary, data=None, evidence=(), has_material=False, latency=None):
    if isinstance(latency, float):
        latency_ms = max(0, int((time.perf_counter() - latency) * 1000))
    else:
        latency_ms = None
    return ToolObservation(
        call_id=call_id, tool=tool, success=success, code=code, summary=summary,
        data=data, evidence=tuple(evidence), has_material=has_material, latency_ms=latency_ms,
    )
