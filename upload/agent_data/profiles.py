"""Valid profile snapshot reads and comparisons."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from psycopg_pool import ConnectionPool

from agent_data.pool import read_transaction
from agent_data.scope import ReadScope, bounded_limit, require_scope


@dataclass(frozen=True, slots=True)
class ProfileSnapshot:
    id: int
    user_id: UUID
    snapshot_type: str
    period_month: str | None
    mood_analysis: str | None
    learning_analysis: str | None
    todo_analysis: str | None
    rhythm_analysis: str | None
    user_tags: Any
    overall_summary: str | None
    embedding: object | None
    created_at: datetime


class ProfileRepository:
    def __init__(self, pool: ConnectionPool):
        self._pool = pool

    def recent(self, scope: ReadScope, limit: int = 2) -> list[ProfileSnapshot]:
        scope = require_scope(scope)
        limit = bounded_limit(limit, maximum=2, default=2)
        with read_transaction(self._pool, scope) as connection:
            rows = connection.execute(
                "SELECT id, user_id, snapshot_type, period_month, mood_analysis, "
                "learning_analysis, todo_analysis, rhythm_analysis, user_tags, overall_summary, "
                "embedding, created_at "
                "FROM mirror_ai.profiles WHERE user_id = %s "
                "ORDER BY CASE snapshot_type WHEN 'manual' THEN 0 WHEN 'monthly' THEN 1 ELSE 2 END, "
                "created_at DESC, id DESC LIMIT %s",
                (scope.user_id, limit),
            ).fetchall()
        return [ProfileSnapshot(**row) for row in rows]

    def compare(
        self, scope: ReadScope, first_id: int, second_id: int
    ) -> tuple[ProfileSnapshot, ProfileSnapshot] | None:
        scope = require_scope(scope)
        first_id = _positive_id(first_id, "first_id")
        second_id = _positive_id(second_id, "second_id")
        if first_id == second_id:
            raise ValueError("two different profile ids are required")
        with read_transaction(self._pool, scope) as connection:
            rows = connection.execute(
                "SELECT id, user_id, snapshot_type, period_month, mood_analysis, "
                "learning_analysis, todo_analysis, rhythm_analysis, user_tags, overall_summary, "
                "embedding, created_at "
                "FROM mirror_ai.profiles WHERE user_id = %s AND id IN (%s, %s)",
                (scope.user_id, first_id, second_id),
            ).fetchall()
        by_id = {row["id"]: ProfileSnapshot(**row) for row in rows}
        if first_id not in by_id or second_id not in by_id:
            return None
        return by_id[first_id], by_id[second_id]


def _positive_id(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 1:
        raise ValueError(f"{name} must be positive")
    return value
