"""Scoped conversation history reads before the current user message."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from psycopg_pool import ConnectionPool

from agent_data.pool import read_transaction
from agent_data.scope import ReadScope, bounded_limit, require_scope


MAX_HISTORY_LIMIT = 50


@dataclass(frozen=True, slots=True)
class HistoryMessage:
    id: int
    user_id: UUID
    session_id: UUID
    role: str
    content: str
    is_fallback: bool
    created_at: datetime


class HistoryRepository:
    def __init__(self, pool: ConnectionPool):
        self._pool = pool

    def recent(self, scope: ReadScope, limit: int) -> list[HistoryMessage]:
        scope = require_scope(scope)
        limit = bounded_limit(limit, maximum=MAX_HISTORY_LIMIT, default=MAX_HISTORY_LIMIT)
        with read_transaction(self._pool, scope) as connection:
            rows = connection.execute(
                "SELECT id, user_id, session_id, role, content, is_fallback, created_at "
                "FROM mirror_ai.chat_history "
                "WHERE user_id = %s AND session_id = %s AND id < %s "
                "AND (role <> 'assistant' OR is_fallback = FALSE) "
                "ORDER BY id DESC LIMIT %s",
                (scope.user_id, scope.session_id, scope.user_message_id, limit),
            ).fetchall()
        rows.reverse()
        return [HistoryMessage(**row) for row in rows]
