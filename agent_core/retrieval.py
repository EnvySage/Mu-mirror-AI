"""Single-route retrieval for the graph_v2 conversation engine."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

from agent_core.types import Evidence, IntentResult, TimeWindow
from agent_data.records import RecordFilters
from agent_data.scope import ReadScope, require_scope


VECTOR_DIMENSION = 1024
VECTOR_DISTANCE_THRESHOLD = 0.35
PROFILE_LIMIT = 2
DIARY_LIMIT = 5


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    route: str
    evidence: tuple[Evidence, ...]
    degraded: bool = False
    degradation_reason: str | None = None
    summary: str = ""


def retrieve(
    intent: IntentResult,
    scope: ReadScope,
    repositories: Any,
    embedder: Any,
    *,
    half_life_days: float = 30,
    limit: int = DIARY_LIMIT,
    deadline: float | None = None,
    reference_time: datetime | None = None,
) -> RetrievalResult:
    """Retrieve one route only; preserve explicit metadata filters on degradation."""
    scope = require_scope(scope)
    if not isinstance(intent, IntentResult):
        raise TypeError("retrieve requires an IntentResult")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")
    limit = min(limit, DIARY_LIMIT)
    if isinstance(half_life_days, bool) or not isinstance(half_life_days, (int, float)) or not math.isfinite(half_life_days) or half_life_days <= 0:
        raise ValueError("half_life_days must be finite and positive")
    if intent.time_expression.kind != "none" and intent.time_window is None:
        raise ValueError("time expression must be normalized before retrieval")

    route = intent.route
    if route == "profile":
        profile_repo = getattr(repositories, "profiles", None)
        month = None
        if intent.time_window is not None and intent.time_window.start is not None:
            month = intent.time_window.start.strftime("%Y-%m")
        profiles = list(profile_repo.recent(scope, limit=PROFILE_LIMIT, month=month) or ()) if profile_repo else []
        if profiles:
            evidence = tuple(_profile_evidence(profile) for profile in profiles[:PROFILE_LIMIT])
            return RetrievalResult("profile", evidence, summary=f"画像快照 {len(evidence)} 份")
        route = "hybrid"
    elif route == "structured":
        structured_filters = _metadata_filters(intent, include_time=True)
        if not structured_filters:
            route = "hybrid"
    elif route == "semantic" and (
        intent.content_type is not None
        or bool(intent.moods)
        or (intent.time_window is not None and (
            intent.time_window.start is not None or intent.time_window.end is not None
        ))
    ):
        # A hard user constraint belongs in the filtered vector path, never in pure semantic mode.
        route = "hybrid"
    elif route not in {"semantic", "hybrid"}:
        route = "hybrid"

    if route == "structured":
        records = getattr(repositories, "records", None)
        if records is None:
            raise TypeError("repositories must provide records")
        chunks = records.search(scope, _record_filter_object(_metadata_filters(intent, include_time=True)), limit=limit)
        evidence = tuple(_chunk_evidence(chunk, "structured") for chunk in chunks)
        return RetrievalResult("structured", evidence, summary=f"结构化检索 {len(evidence)} 段")

    include_metadata = route == "hybrid"
    filters = _metadata_filters(intent, include_time=(route == "semantic" or include_metadata))
    hard_filters = bool(filters)
    records = getattr(repositories, "records", None)
    if records is None:
        raise TypeError("repositories must provide records")
    if embedder is None:
        return _embedding_degraded(records, scope, filters, hard_filters, route, limit, "embedding_unavailable:missing_client")

    timeout = _remaining_timeout(deadline)
    try:
        embed = getattr(embedder, "embed", None)
        if not callable(embed):
            raise TypeError("embedder must provide embed(text)")
        vector = embed(intent.standalone_question, timeout=timeout, max_retries=0)
        if not isinstance(vector, Sequence) or isinstance(vector, (str, bytes)):
            raise ValueError("embedding must be a numeric sequence")
        if len(vector) != VECTOR_DIMENSION:
            return _embedding_degraded(
                records, scope, filters, hard_filters, route, limit, "embedding_dimension_mismatch",
            )
        normalized_vector = []
        for value in vector:
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError("embedding values must be finite numbers") from exc
            if not math.isfinite(number):
                raise ValueError("embedding values must be finite numbers")
            normalized_vector.append(number)
    except TimeoutError:
        raise
    except Exception as exc:
        return _embedding_degraded(
            records, scope, filters, hard_filters, route, limit,
            f"embedding_unavailable:{type(exc).__name__}",
        )

    if reference_time is None:
        reference_time = datetime.now(timezone.utc)
    exact_time = intent.time_window is not None and (
        intent.time_window.start is not None or intent.time_window.end is not None
    )
    chunks = records.search_vector(
        scope,
        normalized_vector,
        _record_filter_object(filters),
        limit=limit,
        threshold=VECTOR_DISTANCE_THRESHOLD,
        half_life_days=float(half_life_days),
        reference_time=reference_time,
        apply_decay=not exact_time,
    )
    evidence = tuple(_chunk_evidence(chunk, route) for chunk in chunks)
    return RetrievalResult(route, evidence, summary=f"{route} 检索 {len(evidence)} 段")


def _metadata_filters(intent: IntentResult, *, include_time: bool) -> dict[str, object]:
    filters: dict[str, object] = {}
    if intent.content_type:
        filters["content_type"] = intent.content_type
    if intent.moods:
        filters["moods"] = intent.moods
    if include_time and intent.time_window is not None:
        if intent.time_window.start is not None:
            filters["start_at"] = intent.time_window.start
        if intent.time_window.end is not None:
            filters["end_at"] = intent.time_window.end
    return filters


def _record_filter_object(values: dict[str, object]) -> RecordFilters:
    allowed = {key: values[key] for key in ("keyword", "content_type", "moods", "start_at", "end_at") if key in values}
    return RecordFilters(**allowed)  # type: ignore[arg-type]


def _embedding_degraded(records, scope, filters, hard_filters, route, limit, reason) -> RetrievalResult:
    evidence: tuple[Evidence, ...] = ()
    if hard_filters:
        chunks = records.search(scope, _record_filter_object(filters), limit=limit)
        evidence = tuple(_chunk_evidence(chunk, "structured_degraded") for chunk in chunks)
    note = "；保留明确过滤条件" if hard_filters else ""
    summary = f"向量检索降级（{reason}）{note}，返回 {len(evidence)} 段"
    return RetrievalResult(route, evidence, degraded=True, degradation_reason=reason, summary=summary)


def _chunk_evidence(chunk, origin: str) -> Evidence:
    return Evidence(
        evidence_id=f"chunk:{chunk.chunk_id}",
        kind="diary",
        record_id=chunk.record_id,
        chunk_id=chunk.chunk_id,
        text=chunk.segment,
        date=chunk.created_at.isoformat() if chunk.created_at else None,
        distance=getattr(chunk, "distance", None),
        origin=origin,
        ranking_score=getattr(chunk, "ranking_score", None),
    )


def _profile_evidence(profile) -> Evidence:
    dimensions = (
        ("mood", getattr(profile, "mood_analysis", None)),
        ("learning", getattr(profile, "learning_analysis", None)),
        ("todo", getattr(profile, "todo_analysis", None)),
        ("rhythm", getattr(profile, "rhythm_analysis", None)),
        ("summary", getattr(profile, "overall_summary", None)),
    )
    text = "\n".join(f"{name}: {value}" for name, value in dimensions if value)
    return Evidence(
        evidence_id=f"profile:{profile.id}", kind="profile", text=text,
        date=profile.created_at.isoformat() if getattr(profile, "created_at", None) else None,
        origin="profile",
    )


def _remaining_timeout(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("graph_v2 deadline expired before embedding")
    return remaining
