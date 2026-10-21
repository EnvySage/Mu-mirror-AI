"""Todo counts from the independently authorized todo projection."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from psycopg_pool import ConnectionPool

from agent_data.pool import read_transaction
from agent_data.scope import ReadScope, require_scope


TODO_STATUSES = ("not_started", "in_progress", "completed")


@dataclass(frozen=True, slots=True)
class TodoStats:
    user_id: UUID
    total: int
    by_status: dict[str, int]


class TodoRepository:
    def __init__(self, pool: ConnectionPool):
        self._pool = pool

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
