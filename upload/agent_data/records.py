"""Authorized diary-chunk queries, statistics, and coverage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Mapping, Sequence
from uuid import UUID

from psycopg_pool import ConnectionPool

from agent_data.pool import read_transaction
from agent_data.scope import ReadScope, bounded_limit, reject_scope_overrides, require_scope


CONTENT_TYPES = frozenset({"todo", "thought", "learning", "plan", "note", "work", "social", "health"})
MOODS = frozenset(
    {
        "happy", "excited", "satisfied", "grateful", "expecting", "calm", "bored",
        "confused", "anxious", "sad", "angry", "exhausted", "stressed",
    }
)
MAX_SEARCH_LIMIT = 20


@dataclass(frozen=True, slots=True)
class RecordWindow:
    """Half-open, timezone-aware window used by record statistics."""

    start_at: datetime | None = None
    end_at: datetime | None = None

    def __post_init__(self) -> None:
        _validate_window(self.start_at, self.end_at)


@dataclass(frozen=True, slots=True)
class RecordFilters:
    keyword: str | None = None
    content_type: str | None = None
    moods: tuple[str, ...] = ()
    start_at: datetime | None = None
    end_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RecordChunk:
    chunk_id: int
    record_id: int
    user_id: UUID
    segment: str
    metadata: dict
    embedding: object | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RecordStats:
    record_count: int
    mood_counts: dict[str, int]
    start_at: datetime | None
    end_at: datetime | None


@dataclass(frozen=True, slots=True)
class Coverage:
    first_date: date | None
    latest_date: date | None
    distinct_dates: int
    record_count: int


def _validate_window(start_at: datetime | None, end_at: datetime | None) -> None:
    for value in (start_at, end_at):
        if value is not None and (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() is None
        ):
            raise ValueError("time bounds must be timezone-aware datetimes")
    if start_at is not None and end_at is not None and start_at >= end_at:
        raise ValueError("time window must have start_at < end_at")


def _window_bounds(window: RecordWindow | Sequence[datetime | None] | None) -> tuple[datetime | None, datetime | None]:
    if window is None:
        return None, None
    if isinstance(window, RecordWindow):
        return window.start_at, window.end_at
    if isinstance(window, Sequence) and not isinstance(window, (str, bytes)) and len(window) == 2:
        start_at, end_at = window
        _validate_window(start_at, end_at)
        return start_at, end_at
    raise TypeError("window must be RecordWindow, a two-item tuple, or None")


def _normalize_filters(filters: RecordFilters | Mapping[str, object] | None) -> RecordFilters:
    if filters is None:
        normalized = RecordFilters()
    elif isinstance(filters, RecordFilters):
        normalized = filters
    elif isinstance(filters, Mapping):
        reject_scope_overrides(filters)
        allowed = {"keyword", "content_type", "moods", "start_at", "end_at"}
        unknown = set(filters) - allowed
        if unknown:
            raise ValueError(f"unsupported record filters: {', '.join(sorted(unknown))}")
        values = dict(filters)
        if "moods" in values:
            values["moods"] = tuple(values["moods"] or ())  # type: ignore[arg-type]
        normalized = RecordFilters(**values)  # type: ignore[arg-type]
    else:
        raise TypeError("filters must be RecordFilters, a mapping, or None")

    if normalized.keyword is not None:
        if not isinstance(normalized.keyword, str):
            raise TypeError("keyword must be a string")
        keyword = normalized.keyword.strip()
        if len(keyword) > 256:
            raise ValueError("keyword must be at most 256 characters")
        normalized = RecordFilters(
            keyword=keyword or None,
            content_type=normalized.content_type,
            moods=normalized.moods,
            start_at=normalized.start_at,
            end_at=normalized.end_at,
        )

    if normalized.content_type is not None and normalized.content_type not in CONTENT_TYPES:
        raise ValueError("content_type is not a supported content type")
    if isinstance(normalized.moods, str):
        raise TypeError("moods must be a sequence of supported mood names")
    moods = tuple(dict.fromkeys(normalized.moods))
    if any(not isinstance(mood, str) or mood not in MOODS for mood in moods):
        raise ValueError("moods contains an unsupported mood")
    _validate_window(normalized.start_at, normalized.end_at)
    return RecordFilters(
        keyword=normalized.keyword,
        content_type=normalized.content_type,
        moods=moods,
        start_at=normalized.start_at,
        end_at=normalized.end_at,
    )


def _append_range(
    conditions: list[str], params: list[object], start_at: datetime | None, end_at: datetime | None
) -> None:
    if start_at is not None:
        conditions.append("created_at >= %s")
        params.append(start_at)
    if end_at is not None:
        conditions.append("created_at < %s")
        params.append(end_at)


class RecordRepository:
    def __init__(self, pool: ConnectionPool):
        self._pool = pool

    def search(
        self,
        scope: ReadScope,
        filters: RecordFilters | Mapping[str, object] | None = None,
        *,
        limit: int = 20,
    ) -> list[RecordChunk]:
        scope = require_scope(scope)
        limit = bounded_limit(limit, maximum=MAX_SEARCH_LIMIT, default=20)
        normalized = _normalize_filters(filters)
        conditions = ["user_id = %s"]
        params: list[object] = [scope.user_id]
        if normalized.keyword:
            conditions.append("strpos(lower(segment), lower(%s)) > 0")
            params.append(normalized.keyword)
        if normalized.content_type:
            conditions.append("metadata->>'contentType' = %s")
            params.append(normalized.content_type)
        if normalized.moods:
            conditions.append(
                "jsonb_exists_any(CASE WHEN jsonb_typeof(metadata->'mood') = 'array' "
                "THEN metadata->'mood' ELSE '[]'::jsonb END, %s::text[])"
            )
            params.append(list(normalized.moods))
        _append_range(conditions, params, normalized.start_at, normalized.end_at)
        params.append(limit)

        sql = (
            "SELECT chunk_id, record_id, user_id, segment, metadata, embedding, created_at "
            "FROM mirror_ai.record_chunks WHERE "
            + " AND ".join(conditions)
            + " ORDER BY created_at DESC, record_id DESC, chunk_id DESC LIMIT %s"
        )
        with read_transaction(self._pool, scope) as connection:
            rows = connection.execute(sql, params).fetchall()
        return [
            RecordChunk(
                chunk_id=row["chunk_id"],
                record_id=row["record_id"],
                user_id=row["user_id"],
                segment=row["segment"],
                metadata=row["metadata"] or {},
                embedding=row["embedding"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def stats(
        self,
        scope: ReadScope,
        window: RecordWindow | Sequence[datetime | None] | None = None,
    ) -> RecordStats:
        scope = require_scope(scope)
        start_at, end_at = _window_bounds(window)
        conditions = ["user_id = %s"]
        params: list[object] = [scope.user_id]
        _append_range(conditions, params, start_at, end_at)
        where = " AND ".join(conditions)
        mood_array = (
            "CASE WHEN jsonb_typeof(metadata->'mood') = 'array' "
            "THEN metadata->'mood' ELSE '[]'::jsonb END"
        )
        with read_transaction(self._pool, scope) as connection:
            count_row = connection.execute(
                f"SELECT count(DISTINCT record_id) AS record_count "
                f"FROM mirror_ai.record_chunks WHERE {where}",
                params,
            ).fetchone()
            mood_rows = connection.execute(
                "SELECT mood, count(*) AS record_count FROM ("
                "SELECT DISTINCT record_id, lower(mood) AS mood "
                "FROM mirror_ai.record_chunks "
                "CROSS JOIN LATERAL jsonb_array_elements_text(" + mood_array + ") AS mood_values(mood) "
                "WHERE " + where + " AND mood_values.mood <> ''"
                ") AS distinct_record_moods GROUP BY mood ORDER BY record_count DESC, mood ASC",
                params,
            ).fetchall()
        return RecordStats(
            record_count=count_row["record_count"],
            mood_counts={row["mood"]: row["record_count"] for row in mood_rows},
            start_at=start_at,
            end_at=end_at,
        )

    def coverage(self, scope: ReadScope, keyword: str) -> Coverage:
        scope = require_scope(scope)
        if not isinstance(keyword, str):
            raise TypeError("keyword must be a string")
        keyword = keyword.strip()
        if not keyword or len(keyword) > 256:
            raise ValueError("keyword must contain 1 to 256 characters")
        with read_transaction(self._pool, scope) as connection:
            row = connection.execute(
                "SELECT min((created_at AT TIME ZONE 'Asia/Shanghai')::date) AS first_date, "
                "max((created_at AT TIME ZONE 'Asia/Shanghai')::date) AS latest_date, "
                "count(DISTINCT (created_at AT TIME ZONE 'Asia/Shanghai')::date) AS distinct_dates, "
                "count(DISTINCT record_id) AS record_count "
                "FROM mirror_ai.record_chunks "
                "WHERE user_id = %s AND strpos(lower(segment), lower(%s)) > 0",
                (scope.user_id, keyword),
            ).fetchone()
        return Coverage(
            first_date=row["first_date"],
            latest_date=row["latest_date"],
            distinct_dates=row["distinct_dates"],
            record_count=row["record_count"],
        )
