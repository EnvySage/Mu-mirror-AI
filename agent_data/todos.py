"""Todo counts from the independently authorized todo projection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from psycopg_pool import ConnectionPool

from agent_data.pool import read_transaction
from agent_data.scope import ReadScope, bounded_limit, require_scope


TODO_STATUSES = ("not_started", "in_progress", "completed")


@dataclass(frozen=True, slots=True)
class TodoStats:
    user_id: UUID
    total: int
    by_status: dict[str, int]


@dataclass(frozen=True, slots=True)
class TodoItem:
    id: int
    user_id: UUID
    title: str
    current_status: str
    source_chunk_id: int
    record_id: int
    created_at: datetime


class TodoRepository:
    def __init__(self, pool: ConnectionPool):
        self._pool = pool

    def list(self, scope: ReadScope, *, status: str | None = None, limit: int = 20,
             offset: int = 0) -> list[TodoItem]:
        scope = require_scope(scope)
        limit = bounded_limit(limit, maximum=20, default=20)
        if status is not None and status not in TODO_STATUSES:
            raise ValueError("status must be a supported todo status")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        conditions = ["user_id = %s"]
        params: list[object] = [scope.user_id]
        if status is not None:
            conditions.append("current_status = %s")
            params.append(status)
        params.extend((limit, offset))
        with read_transaction(self._pool, scope) as connection:
            rows = connection.execute(
                "SELECT id, user_id, title, current_status, source_chunk_id, record_id, created_at "
                "FROM mirror_ai.todos WHERE " + " AND ".join(conditions)
                + " ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s", params,
            ).fetchall()
        return [TodoItem(**row) for row in rows]

    def stats(self, scope: ReadScope) -> TodoStats:
        scope = require_scope(scope)
        with read_transaction(self._pool, scope) as connection:
            rows = connection.execute(
                "SELECT current_status, count(*) AS todo_count "
                "FROM mirror_ai.todos WHERE user_id = %s GROUP BY current_status",
                (scope.user_id,),
            ).fetchall()
        counts = {status: 0 for status in TODO_STATUSES}
        for row in rows:
            status = row["current_status"]
            if status in counts:
                counts[status] = row["todo_count"]
        return TodoStats(user_id=scope.user_id, total=sum(counts.values()), by_status=counts)
